<#
.SYNOPSIS
    Build an EQUAL DOLLAR WEIGHT portfolio across the watchlist - the control case for the
    rebalance feature.

.DESCRIPTION
    Equal weight is not equal risk: ALAB carries roughly sigma=1.06 against SLV's 0.75, and
    LRCX/AMAT are 0.90-correlated near-duplicates. So this book looks perfectly balanced in
    the positions table and badly unbalanced in the risk panel, which is exactly the
    contrast the Risk & Return button exists to show.

    Resets to $10,000 first unless --no-reset. Run with --help for every flag.

.EXAMPLE
    .\scripts\equal_weight_portfolio.ps1                    # whole watchlist, 95% of cash
    .\scripts\equal_weight_portfolio.ps1 --yes              # no confirmation prompt (CI)
    .\scripts\equal_weight_portfolio.ps1 --tickers MU,AMD,SLV
    .\scripts\equal_weight_portfolio.ps1 --invest 0.5 --dry-run
#>

# No [CmdletBinding()] and no param block: every flag belongs to the Python tool, and a
# param block would make PowerShell try to bind --yes itself and fail. $args passes through
# verbatim. $ErrorActionPreference stays at its default too - "Stop" turns the tool's own
# stderr messages into PowerShell exception blobs instead of the one clear line it printed.

. "$PSScriptRoot\lib_portfolio_tool.ps1"

Invoke-PortfolioTool -Command "equal" -Arguments $args
