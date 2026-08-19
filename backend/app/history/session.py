"""The reconstruction, shaped as a session document `POST /api/session` will accept.

STDLIB ONLY - see the note at the top of `ledger.py`.

`POST /api/session` is the only endpoint that can set an exact quantity AND an exact average
cost, in one transaction, under the same `trade_lock()` as a trade. Replaying the ledger as
market buys instead would fill every leg at today's price and rewrite every cost basis and the
cash balance with it - which is precisely the failure `REBALANCE_TEST_HARNESS.md` §6 records
as the reason these endpoints exist at all.

Three validators on the receiving side shape what may be emitted here. All three are correct
and none should be relaxed:

    SessionDocument.cash_balance   ge=0    the sim account cannot buy on margin
    SessionPosition.quantity       gt=0    a zero row is the phantom position a full sell deletes
    _normalize() per position      [A-Z]{1,5}   letters only - AL30, TGNO4 and friends 400

So: carried instruments never become positions, dust quantities are dropped, and the opening
cash rule in `reconstruct.build` is what keeps the balance non-negative.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .reconstruct import DUST, Reconstruction

SESSION_VERSION = 1

# Mirrors market.symbols.TICKER_RE, which POST /api/session applies to every position and
# every watchlist entry. Checked here so the loader reports a clean, named exclusion instead
# of the whole import 400ing on one row.
_MAX_TICKER = 5


def _acceptable(ticker: str) -> bool:
    return ticker.isalpha() and 1 <= len(ticker) <= _MAX_TICKER


def to_session(result: Reconstruction) -> tuple[dict, list[dict]]:
    """`(session document, dropped)`.

    `dropped` names every instrument left out and why, so the CLI can print it rather than
    leaving the user to notice that a book of 35 tickers arrived as 23.
    """
    positions: list[dict] = []
    dropped: list[dict] = []

    for ticker, quantity in sorted(result.positions.items()):
        if quantity <= DUST:
            dropped.append({"ticker": ticker, "reason": "position fully closed"})
            continue
        if not _acceptable(ticker):
            dropped.append({
                "ticker": ticker,
                "reason": "symbol is not a US exchange ticker; POST /api/session would reject it",
            })
            continue
        positions.append({
            "ticker": ticker,
            "quantity": round(quantity, 6),
            "avg_cost": round(max(0.0, result.cost_basis.get(ticker, 0.0)), 6),
        })

    for ticker in result.carried:
        dropped.append({"ticker": ticker, "reason": "no US daily closes; carried at cost only"})

    watchlist = [entry["ticker"] for entry in positions]
    document = {
        "version": SESSION_VERSION,
        "saved_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "cash_balance": round(max(0.0, result.cash_balance), 6),
        "positions": positions,
        "watchlist": watchlist,
        "meta": {
            "source": "reconstructed ledger",
            "as_of": result.as_of.isoformat() if result.as_of else None,
            "opening_cash": round(result.opening_cash, 2),
            "opening_carry": round(result.opening_carry, 2),
            "fx_observations": len(result.fx_points),
            "carried": result.carried,
        },
    }
    return document, dropped
