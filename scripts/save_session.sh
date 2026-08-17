#!/usr/bin/env bash
#
# Save the current portfolio session - cash, positions (with their real average costs) and
# the watchlist - to a JSON file under sessions/.
#
#   ./scripts/save_session.sh                       # -> sessions/default.json
#   ./scripts/save_session.sh --name lopsided       # -> sessions/lopsided.json
#   ./scripts/save_session.sh --file /tmp/x.json
#
# Read-only: it writes a file and touches nothing in the app. Restore with load_session.sh.
#
# The saved document is plain JSON and safe to hand-edit - that is the easiest way to
# author a specific portfolio for a test without trading into it.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib_portfolio_tool.sh
. "$ROOT/scripts/lib_portfolio_tool.sh"

run_portfolio_tool save "$@"
