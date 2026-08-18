#!/usr/bin/env bash
#
# Save the current holdings to an editable TICKER QUANTITY list under suggested/.
#
#   ./scripts/save_list.sh                     # -> suggested/portfolio.txt
#   ./scripts/save_list.sh --name mine         # -> suggested/mine.txt
#
# Read-only. The hand-editable counterpart to save_session.sh, which writes exact JSON.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib_portfolio_tool.sh
. "$ROOT/scripts/lib_portfolio_tool.sh"

run_portfolio_tool dump "$@"
