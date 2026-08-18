#!/usr/bin/env bash
#
# Convert a broker holdings export into a FinAlly weights list.
#
#   ./scripts/import_broker.sh                          # suggested/sugested.txt -> suggested/broker.txt
#   ./scripts/import_broker.sh --source sugested --name bank
#   ./scripts/import_broker.sh --file /tmp/export.txt --name bank
#
# Emits WEIGHTS, not share counts: those holdings are CEDEARs, fractional claims on a US
# share at a per-stock ratio and priced in pesos, so only the proportions carry over.
# Text in, text out - no API call, nothing changed in the app.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib_portfolio_tool.sh
. "$ROOT/scripts/lib_portfolio_tool.sh"

run_portfolio_tool broker "$@"
