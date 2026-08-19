#!/usr/bin/env bash
#
# Reconstruct a real, dated brokerage ledger as a daily USD portfolio curve.
#
#   ./scripts/import_broker_with_dates.sh --dry-run     # print the reconstruction, write nothing
#   ./scripts/import_broker_with_dates.sh               # write backend/calibration/ledger.json
#   ./scripts/import_broker_with_dates.sh --file /tmp/txns.txt --holdings /tmp/positions.txt
#
# Unlike import_broker.sh, which keeps only the proportions, this one keeps the real
# quantities - because with dates the CEDEAR ratios become measurable rather than unknown:
#
#     ratio = us_close(trade date) / cedear price in USD
#
# and the ARS/USD rate needed to put a peso price in dollars falls out of the same-day bond
# conversion pairs already in the file. Nothing external is required.
#
# Text in, text out - no API call. The generated document is committed, the same way
# backend/app/market/seeds.py is, because example/ is not copied into the container image.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib_portfolio_tool.sh
. "$ROOT/scripts/lib_portfolio_tool.sh"

run_portfolio_tool ledger "$@"
