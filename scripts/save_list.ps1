<#
.SYNOPSIS
    Save the current holdings to an editable TICKER QUANTITY list under suggested\.

.DESCRIPTION
    Writes a plain text file you can open and edit: one ticker and one quantity per line.
    Load it back with load_list.ps1, which resets to $10,000 and buys those quantities.

    This is the hand-editable format. save_session.ps1 is the other one - exact JSON,
    including cash and average costs, for reproducing a book precisely rather than
    tinkering with it.

    Read-only: it writes a file and touches nothing in the app.

.EXAMPLE
    .\scripts\save_list.ps1                     # -> suggested\portfolio.txt
    .\scripts\save_list.ps1 --name mine         # -> suggested\mine.txt
    .\scripts\save_list.ps1 --file C:\tmp\x.txt
#>

# See equal_weight_portfolio.ps1 for why there is no param block and no ErrorActionPreference.

. "$PSScriptRoot\lib_portfolio_tool.ps1"

Invoke-PortfolioTool -Command "dump" -Arguments $args
