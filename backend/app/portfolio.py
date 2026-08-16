"""Portfolio valuation, trade execution and the P&L snapshot task.

Trade rules are collected here so the REST path and the LLM's auto-execution path (PLAN.md
§9) share one rule set rather than two that drift.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import sqlite3
import uuid
import weakref
from dataclasses import dataclass, field
from typing import Literal

from . import clock, db
from .market import InvalidTicker, MarketDataService, normalize_ticker
from .schema import DEFAULT_USER, STARTING_CASH

log = logging.getLogger(__name__)

Side = Literal["buy", "sell"]

# Guards trade execution end to end. A single worker does NOT mean serialised: async
# handlers interleave at every await, so a manual trade and an LLM batch can interleave
# between "read cash" and "write cash" and lose an update (Review.md B13).
#
# Created per running loop rather than at import: an `asyncio.Lock()` binds to whichever
# loop first acquires it and then raises "bound to a different event loop" everywhere else.
# Production has one loop for the process lifetime so a module-level lock would appear to
# work, but it makes the object's correctness depend on import order.
_TRADE_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def trade_lock() -> asyncio.Lock:
    """The trade lock for the running loop. All callers on one loop share one lock."""
    loop = asyncio.get_running_loop()
    lock = _TRADE_LOCKS.get(loop)
    if lock is None:
        lock = _TRADE_LOCKS[loop] = asyncio.Lock()
    return lock

SNAPSHOT_INTERVAL_S = 30.0
QUANTITY_DECIMALS = 6           # Review.md B12
DUST = 1e-9                     # below this a position is closed, not fractional (B11)
CASH_EPSILON = 1e-6             # lets a buy that costs exactly the balance through


@dataclass
class TradeResult:
    status: Literal["filled", "rejected"]
    ticker: str
    side: Side
    quantity: float
    reason: str | None = None
    code: str | None = None
    fill_price: float | None = None
    total: float | None = None
    cash_balance: float | None = None
    executed_at: str | None = None
    watchlist_added: bool = False

    @property
    def filled(self) -> bool:
        return self.status == "filled"

    def to_wire(self) -> dict:
        payload = {
            "status": self.status,
            "ticker": self.ticker,
            "side": self.side,
            "quantity": self.quantity,
            "watchlist_added": self.watchlist_added,
        }
        if self.filled:
            payload |= {
                "fill_price": round(self.fill_price, 4),
                "total": round(self.total, 2),
                "cash_balance": round(self.cash_balance, 2),
                "executed_at": self.executed_at,
            }
        else:
            payload |= {"reason": self.reason, "code": self.code}
        return payload


def _rejected(ticker: str, side: str, quantity: float, code: str, reason: str) -> TradeResult:
    return TradeResult(
        status="rejected", ticker=ticker, side=side,  # type: ignore[arg-type]
        quantity=quantity, code=code, reason=reason,
    )


# ---- valuation ---------------------------------------------------------------

def value_portfolio(conn: sqlite3.Connection, service: MarketDataService,
                    user_id: str = DEFAULT_USER) -> dict:
    """Current portfolio state. Never raises on a missing price.

    A position with no cached price is valued at `avg_cost`, not 0: valuing it at 0 renders
    -100% P&L and, worse, writes a garbage snapshot row that permanently corrupts the P&L
    chart (Review.md B16 / design D15). `priced` tells the frontend to render `—` rather
    than a number it should not trust.
    """
    cash = db.cash_balance(conn, user_id)
    rows = db.positions(conn, user_id)

    holdings = []
    for row in rows:
        quantity = float(row["quantity"])
        avg_cost = float(row["avg_cost"])
        live = service.price(row["ticker"])
        price = live if live is not None else avg_cost
        market_value = quantity * price
        cost_basis = quantity * avg_cost
        unrealized = market_value - cost_basis
        holdings.append({
            "ticker": row["ticker"],
            "quantity": quantity,
            "avg_cost": round(avg_cost, 4),
            "price": round(price, 4),
            "priced": live is not None,
            "market_value": round(market_value, 2),
            "cost_basis": round(cost_basis, 2),
            "unrealized_pnl": round(unrealized, 2),
            "unrealized_pnl_pct": round(unrealized / cost_basis * 100.0, 3) if cost_basis else 0.0,
            "updated_at": row["updated_at"],
        })

    positions_value = sum(h["market_value"] for h in holdings)
    for holding in holdings:
        holding["weight"] = (
            round(holding["market_value"] / positions_value, 6) if positions_value else 0.0
        )

    total_value = cash + positions_value
    return {
        "cash_balance": round(cash, 2),
        "positions": holdings,
        "positions_value": round(positions_value, 2),
        "total_value": round(total_value, 2),
        "unrealized_pnl": round(sum(h["unrealized_pnl"] for h in holdings), 2),
        "starting_cash": STARTING_CASH,
        # Realised P&L is folded into cash, so total return is the honest headline number
        # and costs one subtraction (Review.md D6).
        "total_return": round(total_value - STARTING_CASH, 2),
        "total_return_pct": round((total_value - STARTING_CASH) / STARTING_CASH * 100.0, 3),
        "all_priced": all(h["priced"] for h in holdings),
    }


# ---- trade execution ---------------------------------------------------------

def validate_quantity(raw: float) -> tuple[float | None, str | None]:
    """Shared by the REST bar and the LLM path. Returns (quantity, error)."""
    try:
        quantity = float(raw)
    except (TypeError, ValueError):
        return None, "quantity must be a number"
    if not math.isfinite(quantity):
        return None, "quantity must be a finite number"
    quantity = round(quantity, QUANTITY_DECIMALS)
    if quantity <= 0:
        return None, "quantity must be greater than zero"
    if quantity > 1e9:
        return None, "quantity is implausibly large"
    return quantity, None


async def execute_trade(
    ticker: str,
    side: str,
    quantity: float,
    service: MarketDataService,
    user_id: str = DEFAULT_USER,
) -> TradeResult:
    """Market order, instant fill at the cached price. No fees, no partial fills."""
    try:
        symbol = normalize_ticker(ticker)
    except InvalidTicker as exc:
        return _rejected(str(ticker), side, 0.0, "invalid_ticker", str(exc))

    if side not in ("buy", "sell"):
        return _rejected(symbol, side, 0.0, "invalid_side", f"unknown side: {side!r}")

    checked, error = validate_quantity(quantity)
    if checked is None:
        return _rejected(symbol, side, 0.0, "invalid_quantity", error or "invalid quantity")

    # PLAN.md §9: a ticker outside the watchlist is auto-added — which is also what gives
    # it a price to fill at. add_ticker() returns priced, so there is no wait here.
    # Resolved before the lock so a LIVE-mode anchor fetch does not serialise every trade.
    quote = service.quote(symbol)
    if quote is None:
        quote = await service.add_ticker(symbol)
    if quote is None:
        return _rejected(symbol, side, checked, "no_price",
                         f"no market data available for {symbol}")
    fill_price = quote.price

    async with trade_lock():
        result = await db.run(
            lambda conn: _apply(conn, symbol, side, checked, fill_price, user_id)
        )
        if result.filled:
            # A new position joins the tracked set; a closed one may leave it.
            tracked = await db.run(lambda conn: db.tracked_tickers(conn, user_id))
            await service.sync_tracked(tracked)
            await snapshot_now(service, user_id)

    log.info("trade %s: %s %s %s @ %s", result.status, side, checked, symbol, fill_price)
    return result


def _apply(conn: sqlite3.Connection, ticker: str, side: str, quantity: float,
           fill_price: float, user_id: str) -> TradeResult:
    """Read, validate and write inside ONE transaction (Review.md B13)."""
    with db.transaction(conn):
        cash = db.cash_balance(conn, user_id)
        held = db.position(conn, ticker, user_id)
        total = quantity * fill_price
        now = clock.now_iso()

        if side == "buy":
            if total > cash + CASH_EPSILON:
                return _rejected(
                    ticker, side, quantity, "insufficient_cash",
                    f"insufficient cash: {ticker} x{quantity:g} costs ${total:,.2f}, "
                    f"available ${cash:,.2f}",
                )
            cash -= total
            old_quantity = float(held["quantity"]) if held else 0.0
            old_cost = float(held["avg_cost"]) if held else 0.0
            new_quantity = old_quantity + quantity
            new_avg_cost = (old_quantity * old_cost + total) / new_quantity
            _upsert_position(conn, ticker, new_quantity, new_avg_cost, now, user_id)
        else:
            owned = float(held["quantity"]) if held else 0.0
            if quantity > owned + DUST:
                return _rejected(
                    ticker, side, quantity, "insufficient_shares",
                    f"insufficient shares: cannot sell {quantity:g} {ticker}, "
                    f"holding {owned:g}",
                )
            cash += total
            remaining = owned - quantity
            if remaining < DUST:
                # Selling "all" of a position built over several buys otherwise leaves
                # ~4e-16 shares behind: a phantom row that renders in the positions table,
                # pins the ticker in the cache and never goes away (Review.md B11).
                conn.execute(
                    "DELETE FROM positions WHERE user_id = ? AND ticker = ?",
                    (user_id, ticker),
                )
            else:
                # avg_cost is unchanged by sells — only buys re-average.
                _upsert_position(conn, ticker, remaining, float(held["avg_cost"]), now, user_id)

        conn.execute(
            "INSERT INTO trades (id, user_id, ticker, side, quantity, price, executed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), user_id, ticker, side, quantity, fill_price, now),
        )
        conn.execute(
            "UPDATE users_profile SET cash_balance = ? WHERE id = ?", (cash, user_id)
        )
        watchlist_added = side == "buy" and db.add_watchlist(conn, ticker, user_id)

    return TradeResult(
        status="filled", ticker=ticker, side=side,  # type: ignore[arg-type]
        quantity=quantity, fill_price=fill_price, total=total,
        cash_balance=cash, executed_at=now, watchlist_added=watchlist_added,
    )


def _upsert_position(conn: sqlite3.Connection, ticker: str, quantity: float,
                     avg_cost: float, now: str, user_id: str) -> None:
    conn.execute(
        """
        INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (user_id, ticker)
        DO UPDATE SET quantity = excluded.quantity,
                      avg_cost = excluded.avg_cost,
                      updated_at = excluded.updated_at
        """,
        (str(uuid.uuid4()), user_id, ticker, quantity, avg_cost, now),
    )


# ---- P&L snapshots -----------------------------------------------------------

async def snapshot_now(service: MarketDataService, user_id: str = DEFAULT_USER) -> bool:
    """Write one snapshot if the portfolio is fully priced and has actually moved."""
    return await db.run(lambda conn: _snapshot(conn, service, user_id))


def _snapshot(conn: sqlite3.Connection, service: MarketDataService, user_id: str) -> bool:
    state = value_portfolio(conn, service, user_id)
    if not state["all_priced"]:
        # Skip entirely until every position ticker has a real quote, rather than writing
        # an avg_cost-valued row that would sit in the P&L chart forever (Review.md B16).
        return False

    # read-compare-insert in ONE transaction. Two writers reach here concurrently — the
    # 30s snapshot_loop and the post-trade call inside execute_trade, on separate
    # asyncio.to_thread workers — and as three loose statements both read the same
    # last_snapshot, both judged the value "changed" against that stale read, and both
    # inserted: one duplicate point on the P&L chart. BEGIN IMMEDIATE makes the second
    # writer block and re-read after the first commits, so the dedupe below actually sees
    # the row the other thread just wrote (Back_end_review.md P2).
    with db.transaction(conn):
        previous = db.last_snapshot(conn, user_id)
        if previous is not None and (
            abs(float(previous["total_value"]) - state["total_value"]) < 0.005
        ):
            # An idle container would otherwise accumulate 2,880 identical rows a day (C3).
            return False

        db.record_snapshot(conn, state["total_value"], user_id)
    return True


async def snapshot_loop(service: MarketDataService, interval: float = SNAPSHOT_INTERVAL_S,
                        user_id: str = DEFAULT_USER) -> None:
    """Background task: PLAN.md §7 records portfolio value every 30 seconds."""
    while True:
        await asyncio.sleep(interval)
        try:
            await snapshot_now(service, user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("portfolio snapshot failed")


@dataclass
class SnapshotTask:
    """Owns the background snapshot task so lifespan can stop it cleanly."""

    service: MarketDataService
    interval: float = SNAPSHOT_INTERVAL_S
    _task: asyncio.Task | None = field(default=None, init=False, repr=False)

    def start(self) -> None:
        self._task = asyncio.create_task(
            snapshot_loop(self.service, self.interval), name="portfolio-snapshots"
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
