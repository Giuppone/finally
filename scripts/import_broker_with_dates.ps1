<#
.SYNOPSIS
    Reconstruct a real, dated brokerage ledger as a daily USD portfolio curve.

.DESCRIPTION
    Reads a dated transaction export from example\ plus the current-holdings export from
    suggested\, and writes backend\calibration\ledger.json - the committed artifact the app
    reads to draw the portfolio evolution chart.

    Where import_broker.ps1 keeps only the proportions, this keeps the real quantities. With
    dates that becomes possible, because every CEDEAR ratio is measurable from the file
    itself:

        ratio = us_close(trade date) / cedear price in USD

    and the ARS/USD rate needed to turn a peso price into a dollar one is the ratio of the
    same-day bond conversion pairs already sitting in the ledger. No ratio table, no external
    exchange-rate series, nothing to look up.

    Everything it prints is auditable before you commit the result: the measured rate on each
    date, the ratio for each ticker and where it came from, the back-solved opening book, and
    a reconciliation check that the walked positions end exactly where the holdings file says
    they should.

    Text in, text out: it calls no API and changes nothing in the running app.

.EXAMPLE
    .\scripts\import_broker_with_dates.ps1 --dry-run      # print it, write nothing
    .\scripts\import_broker_with_dates.ps1                # write the document
    .\scripts\import_broker_with_dates.ps1 --file C:\tmp\txns.txt --holdings C:\tmp\pos.txt

    # then restart the app, and optionally adopt the book as the live portfolio:
    .\scripts\start_windows.ps1
    .\scripts\load_history.ps1 --dry-run
#>

# See equal_weight_portfolio.ps1 for why there is no param block and no ErrorActionPreference.

. "$PSScriptRoot\lib_portfolio_tool.ps1"

Invoke-PortfolioTool -Command "ledger" -Arguments $args
