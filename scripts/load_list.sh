#!/usr/bin/env bash
#
# Load a TICKER QUANTITY list as the portfolio: reset to $10,000, then buy it.
#
#   ./scripts/load_list.sh --yes                   # <- suggested/portfolio.txt
#   ./scripts/load_list.sh --name mine --yes
#   ./scripts/load_list.sh --name mine --dry-run   # print the plan, write nothing
#
# Buys at market like equal_weight_portfolio.sh does, so the book is built through the same
# validation a person clicking Buy goes through. The cost is checked against the balance
# before the first order, so an over-expensive list fails cleanly.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib_portfolio_tool.sh
. "$ROOT/scripts/lib_portfolio_tool.sh"

run_portfolio_tool build "$@"
