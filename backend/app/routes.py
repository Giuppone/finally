"""Watchlist and portfolio HTTP surface (PLAN.md §8).

Wire convention is snake_case, matching Python and the DB; the frontend maps once at its
fetch layer (Review.md A1/D17). Timestamps here are ISO 8601 UTC — epoch milliseconds are
confined to the market-data endpoints (B14).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from . import clock, db, portfolio
from .market import InvalidTicker, MarketDataService, get_service, normalize_ticker

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["trading"])

MAX_HISTORY_POINTS = 2_000

# Bumped only when a saved document stops being readable as-is. `POST /api/session`
# rejects anything else rather than guessing at a shape it does not know.
SESSION_VERSION = 1


class WatchlistAdd(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=12)


class TradeRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=12)
    quantity: float
    side: str = Field(..., pattern="^(buy|sell)$")


class RebalanceLeg(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=12)
    quantity: float
    side: str = Field(..., pattern="^(buy|sell)$")


class RebalanceExecution(BaseModel):
    """Exactly the `trades` array `POST /api/analytics/rebalance` returns, so the frontend
    can hand the suggestion straight back without reshaping it."""

    trades: list[RebalanceLeg] = Field(default_factory=list)


class SessionPosition(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=12)
    # gt=0: a zero-quantity row is the phantom position `_apply` deletes on a full sell
    # (Review.md B11), and restoring one would put it straight back.
    quantity: float = Field(..., gt=0)
    avg_cost: float = Field(..., ge=0)


class SessionDocument(BaseModel):
    """What `GET /api/session` emits and `POST /api/session` accepts.

    `meta` is accepted and ignored — it is there so a saved file is readable by a human,
    not so the import can trust it.
    """

    version: int = SESSION_VERSION
    cash_balance: float = Field(..., ge=0)
    positions: list[SessionPosition] = Field(default_factory=list)
    watchlist: list[str] = Field(default_factory=list)
    saved_at: str | None = None
    meta: dict | None = None


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


@router.post("/portfolio/rebalance")
async def post_rebalance(
    body: RebalanceExecution,
    service: MarketDataService = Depends(get_service),
) -> dict:
    """Execute a trade list from `POST /api/analytics/rebalance`.

    Separate from the suggestion endpoint on purpose: a button labelled "suggest" must not
    trade. The whole batch runs under one hold of `trade_lock()` so nothing interleaves
    between the sells and the buys - see `portfolio.execute_batch`.
    """
    if not body.trades:
        raise HTTPException(400, "no trades to execute")

    legs = [(leg.ticker, leg.side, leg.quantity) for leg in body.trades]
    results = await portfolio.execute_batch(legs, service)
    state = await db.run(lambda conn: portfolio.value_portfolio(conn, service))
    return {
        "trades": [result.to_wire() for result in results],
        "filled": sum(1 for result in results if result.filled),
        "rejected": sum(1 for result in results if not result.filled),
        "portfolio": state,
    }


# ---- portfolio sessions ------------------------------------------------------

@router.get("/session")
async def get_session(service: MarketDataService = Depends(get_service)) -> dict:
    """Export the portfolio as a document that `POST /api/session` restores exactly.

    Cash, quantities and average costs are emitted **unrounded**. A save file exists to
    round-trip a state, and rounding cash to the cent turns "restore" into "restore, minus
    a few cents that then compound through every later trade".

    `meta` is informational only and is ignored on import: prices move, so a document
    loaded tomorrow will not reproduce today's `total_value` and should not pretend to.
    """

    def read(conn) -> dict:
        state = portfolio.value_portfolio(conn, service)
        return {
            "version": SESSION_VERSION,
            "saved_at": clock.now_iso(),
            "cash_balance": db.cash_balance(conn),
            "positions": [
                {
                    "ticker": row["ticker"],
                    "quantity": float(row["quantity"]),
                    "avg_cost": float(row["avg_cost"]),
                }
                for row in db.positions(conn)
            ],
            "watchlist": [row["ticker"] for row in db.watchlist(conn)],
            "meta": {
                "mode": str(service.mode),
                "total_value": state["total_value"],
                "starting_cash": state["starting_cash"],
                "all_priced": state["all_priced"],
            },
        }

    return await db.run(read)


@router.post("/session")
async def post_session(
    document: SessionDocument,
    service: MarketDataService = Depends(get_service),
) -> dict:
    """Restore a saved portfolio: cash, positions and watchlist, replacing what is there.

    Same locking as `/portfolio/reset`, for the same reason — an in-flight trade past its
    price read would otherwise commit on top of the restored tables, and the tracked-set
    read has to be inside too or it can evict a ticker whose position still exists.

    Positions are restored with their saved `avg_cost`, which is why this exists as an
    endpoint at all: replaying a session as market buys would fill at today's price and
    silently rewrite every cost basis and the cash balance with it.
    """
    if document.version != SESSION_VERSION:
        raise HTTPException(
            400,
            f"unsupported session version {document.version}; this build reads "
            f"version {SESSION_VERSION}",
        )

    holdings: list[tuple[str, float, float]] = []
    seen: set[str] = set()
    for entry in document.positions:
        symbol = _normalize(entry.ticker)
        if symbol in seen:
            # Two rows for one ticker has no single correct reading — merging them would
            # invent an average cost the user never held.
            raise HTTPException(400, f"duplicate position for {symbol}")
        seen.add(symbol)
        holdings.append((symbol, entry.quantity, entry.avg_cost))

    tickers = sorted({_normalize(raw) for raw in document.watchlist})

    async with portfolio.trade_lock():
        await db.run(
            lambda conn: db.import_session(
                conn, cash=document.cash_balance, holdings=holdings, tickers=tickers
            )
        )
        tracked = await db.run(db.tracked_tickers)
        await service.sync_tracked(tracked)
        state = await db.run(lambda conn: portfolio.value_portfolio(conn, service))
        # The restored account is a new starting point for the P&L chart, which the import
        # just emptied. Without this the chart stays blank until the 30s task next fires.
        await portfolio.snapshot_now(service)

    log.info(
        "session loaded: %d positions, %d watched, cash %.2f",
        len(holdings), len(tickers), document.cash_balance,
    )
    return {
        "loaded": {"positions": len(holdings), "watchlist": len(tickers)},
        "unpriced": [h["ticker"] for h in state["positions"] if not h["priced"]],
        "portfolio": state,
    }
