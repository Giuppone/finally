"""Watchlist and portfolio HTTP surface (PLAN.md §8).

Wire convention is snake_case, matching Python and the DB; the frontend maps once at its
fetch layer (Review.md A1/D17). Timestamps here are ISO 8601 UTC — epoch milliseconds are
confined to the market-data endpoints (B14).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from . import db, portfolio
from .market import InvalidTicker, MarketDataService, get_service, normalize_ticker

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["trading"])

MAX_HISTORY_POINTS = 2_000


class WatchlistAdd(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=12)


class TradeRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=12)
    quantity: float
    side: str = Field(..., pattern="^(buy|sell)$")


def _normalize(raw: str) -> str:
    try:
        return normalize_ticker(raw)
    except InvalidTicker as exc:
        raise HTTPException(400, str(exc)) from exc


# ---- watchlist ---------------------------------------------------------------

@router.get("/watchlist")
async def get_watchlist(service: MarketDataService = Depends(get_service)) -> dict:
    """Watched tickers with their latest prices. A ticker with no quote yet reports
    `priced: false` and null prices — the frontend renders `—`, never `$0.00`, which would
    show as a -100% daily change (Review.md B2)."""
    rows = await db.run(db.watchlist)
    entries = []
    for row in rows:
        quote = service.quote(row["ticker"])
        entry = {"ticker": row["ticker"], "added_at": row["added_at"],
                 "priced": quote is not None}
        entries.append(entry | (quote.to_wire() if quote else {}))
    return {"tickers": entries, "mode": str(service.mode)}


@router.post("/watchlist", status_code=201)
async def add_to_watchlist(
    body: WatchlistAdd,
    service: MarketDataService = Depends(get_service),
) -> dict:
    ticker = _normalize(body.ticker)

    if not await service.validate(ticker):          # free in the Massive modes (D9)
        raise HTTPException(400, f"unknown symbol: {ticker}")

    quote = await service.add_ticker(ticker)        # priced before we return (D10)
    if quote is None:
        raise HTTPException(502, f"no market data available for {ticker}")

    added = await db.run(lambda conn: db.add_watchlist(conn, ticker))
    return {"ticker": ticker, "added": added, "priced": True} | quote.to_wire()


@router.delete("/watchlist/{ticker}", status_code=204)
async def remove_from_watchlist(
    ticker: str,
    service: MarketDataService = Depends(get_service),
) -> None:
    symbol = _normalize(ticker)
    await db.run(lambda conn: db.remove_watchlist(conn, symbol))
    # The UNION is the point: a ticker with an open position stays in the cache and keeps
    # ticking even though it just left the watchlist (PLAN.md §13 item 4).
    tracked = await db.run(db.tracked_tickers)
    await service.sync_tracked(tracked)


# ---- portfolio ---------------------------------------------------------------

@router.get("/portfolio")
async def get_portfolio(service: MarketDataService = Depends(get_service)) -> dict:
    return await db.run(lambda conn: portfolio.value_portfolio(conn, service))


@router.post("/portfolio/trade")
async def post_trade(
    body: TradeRequest,
    service: MarketDataService = Depends(get_service),
) -> dict:
    result = await portfolio.execute_trade(body.ticker, body.side, body.quantity, service)
    if not result.filled:
        # Structured detail, so the frontend can branch on `code` instead of parsing prose.
        raise HTTPException(400, result.to_wire())
    state = await db.run(lambda conn: portfolio.value_portfolio(conn, service))
    return {"trade": result.to_wire(), "portfolio": state}


@router.get("/portfolio/history")
async def get_portfolio_history(
    since: str | None = Query(None, description="ISO 8601 UTC lower bound, inclusive"),
    limit: int = Query(500, ge=1, le=MAX_HISTORY_POINTS),
    service: MarketDataService = Depends(get_service),
) -> dict:
    """P&L chart data. Bounded by default — 30s snapshots are 2,880 rows a day and an
    unbounded chart would eventually plot a million points (Review.md C3)."""
    rows = await db.run(lambda conn: db.snapshots(conn, since=since, limit=limit))
    return {
        "points": [
            {"recorded_at": row["recorded_at"], "total_value": round(row["total_value"], 2)}
            for row in rows
        ],
        "starting_cash": portfolio.STARTING_CASH,
    }


@router.post("/portfolio/reset")
async def post_reset(service: MarketDataService = Depends(get_service)) -> dict:
    """Back to $10k, the seed watchlist and no history.

    E2E needs it — the fresh-start scenario fails on the second run against a persisted
    volume otherwise — and it is the escape hatch when a demo's LLM drains the account
    (Review.md B9).

    The whole operation runs under `trade_lock()` because `execute_trade` takes that same
    lock, and a lock only one side takes serialises nothing (Back_end_review.md P1).
    Unlocked, a trade already past its price read could commit ON TOP of the just-cleared
    tables — leaving a position and a cash balance ≠ $10,000 inside what this response
    reports as a fresh account. The tracked-set read and `sync_tracked` are inside too:
    reading between another trade's commit and its own `sync_tracked` yields a set that
    evicts a ticker whose position still exists, silently freezing its price.
    """
    async with portfolio.trade_lock():
        await db.run(db.reset)
        tracked = await db.run(db.tracked_tickers)
        await service.sync_tracked(tracked)
        return await db.run(lambda conn: portfolio.value_portfolio(conn, service))
