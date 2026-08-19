#!/usr/bin/env bash
#
# Adopt the reconstructed real book as the live FinAlly portfolio.
#
#   ./scripts/load_history.sh --dry-run     # print the book, change nothing
#   ./scripts/load_history.sh --yes         # replace the portfolio
#
# Needs the app running and backend/calibration/ledger.json generated
# (./scripts/import_broker_with_dates.sh).
#
# Goes through POST /api/session, not a sequence of market buys: only that endpoint can set
# an exact quantity AND an exact average cost. Replaying the ledger as buys would fill every
# leg at today's price and silently rewrite every cost basis.
#
# It replaces positions, trades and P&L history (chat history is kept), so it prompts unless
# --yes.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib_portfolio_tool.sh
. "$ROOT/scripts/lib_portfolio_tool.sh"

run_portfolio_tool load_history "$@"
