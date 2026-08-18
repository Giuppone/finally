<#
.SYNOPSIS
    Load a TICKER QUANTITY list as the portfolio: reset to $10,000, then buy it.

.DESCRIPTION
    Works the same way equal_weight_portfolio.ps1 does - it resets and then BUYS at the
    current price, so the book is built exactly as a person would build it, through
    validation and the trade blotter. The cost is checked against the balance before the
    first order goes in, so a list that is too expensive fails cleanly instead of leaving a
    half-built portfolio.

    Unknown tickers are added to the watchlist first, which is what gives them a price.

.EXAMPLE
    .\scripts\load_list.ps1 --yes                    # <- suggested\portfolio.txt
    .\scripts\load_list.ps1 --name mine --yes
    .\scripts\load_list.ps1 --name mine --dry-run    # print the plan, write nothing
    .\scripts\load_list.ps1 --name mine --yes --no-reset   # buy on top of what is there
#>

# See equal_weight_portfolio.ps1 for why there is no param block and no ErrorActionPreference.

. "$PSScriptRoot\lib_portfolio_tool.ps1"

Invoke-PortfolioTool -Command "build" -Arguments $args
