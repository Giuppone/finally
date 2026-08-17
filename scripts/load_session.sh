#!/usr/bin/env bash
#
# Restore a portfolio session saved by save_session.sh.
#
#   ./scripts/load_session.sh                       # <- sessions/default.json
#   ./scripts/load_session.sh --name lopsided --yes
#   ./scripts/load_session.sh --file /tmp/x.json
#
# Positions come back with their SAVED average costs, not today's prices - which is why
# this goes through POST /api/session rather than replaying the trades. Replaying would
# fill at the current price and silently rewrite every cost basis and the cash balance.
#
# Replaces positions, the watchlist, the trade blotter and the P&L history. Chat history
# is kept - this restores a portfolio, not the whole app. Use POST /api/portfolio/reset
# for the full wipe.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib_portfolio_tool.sh
. "$ROOT/scripts/lib_portfolio_tool.sh"

run_portfolio_tool load "$@"
