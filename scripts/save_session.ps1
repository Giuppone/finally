<#
.SYNOPSIS
    Save the current portfolio session - cash, positions (with their real average costs)
    and the watchlist - to a JSON file under sessions\.

.DESCRIPTION
    Read-only: it writes a file and touches nothing in the app. Restore with
    load_session.ps1.

    The saved document is plain JSON and safe to hand-edit - that is the easiest way to
    author a specific portfolio for a test without trading into it.

.EXAMPLE
    .\scripts\save_session.ps1                       # -> sessions\default.json
    .\scripts\save_session.ps1 --name lopsided       # -> sessions\lopsided.json
    .\scripts\save_session.ps1 --file C:\tmp\x.json
#>

# See equal_weight_portfolio.ps1 for why there is no param block and no ErrorActionPreference.

. "$PSScriptRoot\lib_portfolio_tool.ps1"

Invoke-PortfolioTool -Command "save" -Arguments $args
