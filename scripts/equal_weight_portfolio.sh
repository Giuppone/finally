#!/usr/bin/env bash
#
# Build an EQUAL DOLLAR WEIGHT portfolio across the watchlist - the control case for the
# rebalance feature.
#
# Equal weight is not equal risk: ALAB carries roughly sigma=1.06 against SLV's 0.75, and
# LRCX/AMAT are 0.90-correlated near-duplicates. So this book looks perfectly balanced in
# the positions table and badly unbalanced in the risk panel, which is exactly the contrast
# the Risk & Return button exists to show.
#
#   ./scripts/equal_weight_portfolio.sh                    # whole watchlist, 95% of cash
#   ./scripts/equal_weight_portfolio.sh --yes              # no confirmation prompt (CI)
#   ./scripts/equal_weight_portfolio.sh --tickers MU,AMD,SLV
#   ./scripts/equal_weight_portfolio.sh --invest 0.5 --dry-run
#
# Resets to $10,000 first unless --no-reset. Run --help for every flag.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib_portfolio_tool.sh
. "$ROOT/scripts/lib_portfolio_tool.sh"

run_portfolio_tool equal "$@"
