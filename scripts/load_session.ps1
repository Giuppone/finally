<#
.SYNOPSIS
    Restore a portfolio session saved by save_session.ps1.

.DESCRIPTION
    Positions come back with their SAVED average costs, not today's prices - which is why
    this goes through POST /api/session rather than replaying the trades. Replaying would
    fill at the current price and silently rewrite every cost basis and the cash balance.

    Replaces positions, the watchlist, the trade blotter and the P&L history. Chat history
    is kept - this restores a portfolio, not the whole app. Use POST /api/portfolio/reset
    for the full wipe.

.EXAMPLE
    .\scripts\load_session.ps1                       # <- sessions\default.json
    .\scripts\load_session.ps1 --name lopsided --yes
    .\scripts\load_session.ps1 --file C:\tmp\x.json
#>

# See equal_weight_portfolio.ps1 for why there is no param block and no ErrorActionPreference.

. "$PSScriptRoot\lib_portfolio_tool.ps1"

Invoke-PortfolioTool -Command "load" -Arguments $args
