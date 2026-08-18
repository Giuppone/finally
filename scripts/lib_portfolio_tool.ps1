<#
.SYNOPSIS
    Shared runner for the portfolio harness scripts. Dot-sourced, never run directly.

.DESCRIPTION
    Finds a Python that can run backend/scripts/portfolio_tool.py and hands off to it. The
    tool is stdlib-only, so a bare interpreter is enough - uv is a fallback, not a
    requirement, and a host with neither still works through the container.

    This is the PowerShell twin of lib_portfolio_tool.sh. Both are thin: all the logic
    lives in the Python file, so the two shells cannot drift apart.
#>

# Captured while this file is being dot-sourced rather than read inside the function:
# $PSScriptRoot inside a dot-sourced function is easy to get wrong. Both files live in
# scripts\, so this is the repo root either way.
$FinallyRepoRoot = Split-Path -Parent $PSScriptRoot

function Get-FinallyRunner {
    param([string]$Root)

    $tool = Join-Path $Root "backend\scripts\portfolio_tool.py"

    # A bare interpreter first: `uv run` re-checks the lockfile on every invocation, which
    # is wasted work for a script that imports nothing outside the stdlib.
    foreach ($candidate in @("python3", "python")) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($null -eq $command) { continue }
        # Windows ships a Store stub named python.exe that exits without running anything,
        # so probe the version rather than trusting that the name resolves.
        & $command.Source -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" *> $null
        if ($LASTEXITCODE -eq 0) {
            return @($command.Source, $tool)
        }
    }

    if ((Get-Command uv -ErrorAction SilentlyContinue) -and
        (Test-Path (Join-Path $Root "backend\pyproject.toml"))) {
        return @("uv", "--directory", (Join-Path $Root "backend"), "run", "python",
                 "scripts\portfolio_tool.py")
    }

    # No Python on the host. The image already carries backend/scripts at /app/scripts
    # (the Dockerfile's `COPY backend/ ./`), so the running container can do it.
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        $running = docker ps -q -f "name=^finally$" 2>$null
        if (-not [string]::IsNullOrWhiteSpace($running)) {
            return @("docker", "exec", "-i", "finally", "python",
                     "/app/scripts/portfolio_tool.py")
        }
    }

    return $null
}

function Invoke-PortfolioTool {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        # [object[]], not [string[]]: see the flattening below. Binding to [string[]] is
        # what destroys a comma-separated value, and it happens before any code here runs.
        [object[]]$Arguments = @()
    )

    # In PowerShell the comma is the ARRAY operator, so `--tickers MU,AMD,SLV` reaches this
    # function as a nested array, not as one string. Stringified the usual way that becomes
    # "MU AMD SLV" - space-joined - and the tool then looks up a single ticker by that name
    # and reports it unpriced. Re-joining with commas reproduces exactly what was typed, and
    # leaves every other argument untouched.
    $flat = @(foreach ($argument in $Arguments) {
        if ($argument -is [System.Array]) {
            ($argument | ForEach-Object { "$_" }) -join ","
        }
        else {
            "$argument"
        }
    })

    $runner = Get-FinallyRunner -Root $FinallyRepoRoot

    if ($null -eq $runner) {
        Write-Error @"
No way to run the portfolio tool on this machine.
Install Python 3.9+, or start the container first:
  .\scripts\start_windows.ps1        # then re-run this script
"@
        exit 1
    }

    $argv = @($runner[1..($runner.Length - 1)]) + @($Command)

    if ($runner[0] -eq "docker") {
        # Sessions are files, and inside the container the default sessions\ path resolves
        # to /sessions - ephemeral, and invisible from the host. The seeders are fine over
        # docker exec because they only talk HTTP; save and load are not.
        if ($Command -in @("save", "load", "dump", "build", "broker")) {
            Write-Error @"
save/load need Python on the host: they read and write files under sessions\,
and the container's filesystem is not the host's. Install Python 3.9+ (or run the
tool yourself with an explicit --file inside the container).
"@
            exit 1
        }
        # Prepended, not appended, so a user-supplied --base still wins: argparse takes
        # the last occurrence.
        $argv += @("--base", "http://127.0.0.1:8000")
    }

    & $runner[0] @argv @flat
    exit $LASTEXITCODE
}
