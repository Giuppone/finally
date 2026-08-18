<#
.SYNOPSIS
    Convert a broker holdings export into a FinAlly weights list.

.DESCRIPTION
    Reads an Argentine broker export from suggested\, and writes a TICKER WEIGHT% list you
    can review, edit and then load with load_list.ps1.

    WEIGHTS, not share counts, and deliberately so. Those holdings are CEDEARs -
    certificates over a fraction of a US share, at a ratio that differs per stock, priced in
    pesos. 100 MU CEDEARs is not 100 MU shares, and a nine-figure peso book is not a $10,000
    one. The only thing that carries over meaningfully is the proportions.

    Locally-listed rows with no US ticker are dropped and named in the file header; pass
    --keep-local to keep them.

    Text in, text out: it calls no API and changes nothing in the app.

.EXAMPLE
    .\scripts\import_broker.ps1                              # suggested\sugested.txt -> suggested\broker.txt
    .\scripts\import_broker.ps1 --source sugested --name bank
    .\scripts\import_broker.ps1 --file C:\tmp\export.txt --name bank

    # then
    .\scripts\load_list.ps1 --name broker --dry-run
    .\scripts\load_list.ps1 --name broker --yes
#>

# See equal_weight_portfolio.ps1 for why there is no param block and no ErrorActionPreference.

. "$PSScriptRoot\lib_portfolio_tool.ps1"

Invoke-PortfolioTool -Command "broker" -Arguments $args
