<#
.SYNOPSIS
    Stop FinAlly (Windows). Idempotent, and it never removes the data volume — the
    portfolio, trade history and conversation are meant to survive a restart (PLAN.md §11).

.NOTES
    To wipe the data too, do it explicitly:  docker volume rm finally-data
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Container = "finally"

docker info *> $null
if (-not $?) {
    Write-Host "Docker is not running; nothing to stop."
    exit 0
}

$existing = docker ps -aq -f "name=^$Container$"
if ([string]::IsNullOrWhiteSpace($existing)) {
    Write-Host "No $Container container found; nothing to stop."
    exit 0
}

docker rm -f $Container *> $null
Write-Host "Stopped and removed $Container. The finally-data volume was kept."
