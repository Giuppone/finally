#!/usr/bin/env bash
#
# Build a RANDOM, deliberately lopsided portfolio - the input the rebalance feature has
# something large and obvious to fix.
#
# Weights come from a Dirichlet(0.6) draw, so most of the money lands in one or two names.
# The draw is seeded (default 42), so the same command always produces the same book -
# a harness that seeds a different portfolio every run cannot assert on anything.
#
#   ./scripts/start_random_portfolio.sh                    # 6 names, seed 42, 95% of cash
#   ./scripts/start_random_portfolio.sh --yes --seed 7     # a different, still fixed, book
#   ./scripts/start_random_portfolio.sh --count 3 --concentration 0.3   # very concentrated
#   ./scripts/start_random_portfolio.sh --dry-run          # print the plan, write nothing
#
# Resets to $10,000 first unless --no-reset. Run --help for every flag.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib_portfolio_tool.sh
. "$ROOT/scripts/lib_portfolio_tool.sh"

run_portfolio_tool random "$@"
