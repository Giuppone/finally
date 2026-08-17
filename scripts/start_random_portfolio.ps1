<#
.SYNOPSIS
    Build a RANDOM, deliberately lopsided portfolio - the input the rebalance feature has
    something large and obvious to fix.

.DESCRIPTION
    Weights come from a Dirichlet(0.6) draw, so most of the money lands in one or two
    names. The draw is seeded (default 42), so the same command always produces the same
    book - a harness that seeds a different portfolio every run cannot assert on anything.

    Resets to $10,000 first unless --no-reset. Run with --help for every flag.

.EXAMPLE
    .\scripts\start_random_portfolio.ps1                    # 6 names, seed 42, 95% of cash
    .\scripts\start_random_portfolio.ps1 --yes --seed 7     # different, still fixed, book
    .\scripts\start_random_portfolio.ps1 --count 3 --concentration 0.3
    .\scripts\start_random_portfolio.ps1 --dry-run          # print the plan, write nothing
#>

# See equal_weight_portfolio.ps1 for why there is no param block and no ErrorActionPreference.

. "$PSScriptRoot\lib_portfolio_tool.ps1"

Invoke-PortfolioTool -Command "random" -Arguments $args
