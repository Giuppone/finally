"""Auto-execution of the actions in an LLM reply (PLAN.md §9).

Everything here goes through `portfolio.execute_trade` and the same `db` helpers the REST
routes use. The LLM gets no privileged path: an unaffordable buy is rejected for the model
exactly as it is for the trade bar, which is the whole reason those rules live in
`portfolio.py` rather than in the route handler.
"""

from __future__ import annotations

import logging

from .. import db, portfolio
from ..market import InvalidTicker, MarketDataService, normalize_ticker
from ..schema import DEFAULT_USER
from .models import ActionRecord, AssistantReply, TradeIntent, WatchlistIntent

log = logging.getLogger(__name__)


async def execute(
    reply: AssistantReply,
    service: MarketDataService,
    user_id: str = DEFAULT_USER,
) -> list[ActionRecord]:
    """Run the reply's actions and report what actually happened.

    Trades run before watchlist changes so that "sell all my AMD and stop watching it"
    lands in that order rather than de-listing the ticker out from under its own sale.
    """
    records = await _run_trades(reply.trades, service, user_id)
    records += await _run_watchlist(reply.watchlist_changes, service, user_id)
    return records


async def _run_trades(
    intents: list[TradeIntent], service: MarketDataService, user_id: str
) -> list[ActionRecord]:
    """Sequential, in array order, halting at the first rejection (PLAN.md §9, §13 item 5).

    Sequential because each trade must be validated against the balance its predecessors
    left behind: validating a batch against one pre-response snapshot lets two individually
    affordable buys both pass and drive cash negative.

    Halting because a batch is a *plan* — "sell AMD, then buy MU with the proceeds" has a
    second leg that is unfunded the moment the first leg fails. Executing it anyway would
    turn a failed plan into an unrequested position. Earlier trades stand; the rejection and
    everything after it come back with a reason, so the model can explain the partial fill.
    """
    records: list[ActionRecord] = []
    halted = False

    for intent in intents:
        if halted:
            records.append(ActionRecord(
                kind="trade", status="skipped", ticker=intent.ticker.upper(),
                side=intent.side, quantity=intent.quantity, code="batch_halted",
                detail="Skipped: an earlier trade in this batch was rejected.",
            ))
            continue

        result = await portfolio.execute_trade(
            intent.ticker, intent.side, intent.quantity, service, user_id
        )

        if result.filled:
            records.append(ActionRecord(
                kind="trade", status="executed", ticker=result.ticker, side=result.side,
                quantity=result.quantity, fill_price=round(result.fill_price, 4),
                total=round(result.total, 2),
                detail=(
                    f"{result.side.capitalize()} {result.quantity:g} {result.ticker} "
                    f"@ ${result.fill_price:,.2f} = ${result.total:,.2f}"
                ),
            ))
        else:
            halted = True
            records.append(ActionRecord(
                kind="trade", status="rejected", ticker=result.ticker, side=result.side,
                quantity=result.quantity, code=result.code,
                detail=result.reason or "Trade rejected.",
            ))

    return records


async def _run_watchlist(
    intents: list[WatchlistIntent], service: MarketDataService, user_id: str
) -> list[ActionRecord]:
    """Adds and removes. One bad symbol is reported and skipped, never fatal to the batch."""
    records: list[ActionRecord] = []

    for intent in intents:
        try:
            symbol = normalize_ticker(intent.ticker)
        except InvalidTicker as exc:
            records.append(ActionRecord(
                kind="watchlist", status="rejected", ticker=intent.ticker.upper(),
                action=intent.action, code="invalid_ticker", detail=str(exc),
            ))
            continue

        if intent.action == "add":
            records.append(await _add(symbol, service, user_id))
        else:
            removed = await db.run(
                lambda conn: db.remove_watchlist(conn, symbol, user_id)
            )
            # The position (if any) survives and keeps ticking — tracked_tickers is the
            # union of the watchlist and open positions (PLAN.md §6, §13 item 4).
            tracked = await db.run(lambda conn: db.tracked_tickers(conn, user_id))
            await service.sync_tracked(tracked)
            records.append(ActionRecord(
                kind="watchlist", status="executed" if removed else "skipped",
                ticker=symbol, action="remove",
                detail=(f"Removed {symbol} from the watchlist." if removed
                        else f"{symbol} was not on the watchlist."),
            ))

    return records


async def _add(symbol: str, service: MarketDataService, user_id: str) -> ActionRecord:
    """Mirrors `POST /api/watchlist`: validate at the provider, price it, then persist."""
    if not await service.validate(symbol):
        return ActionRecord(
            kind="watchlist", status="rejected", ticker=symbol, action="add",
            code="unknown_symbol", detail=f"{symbol} is not a symbol the provider knows.",
        )

    quote = await service.add_ticker(symbol)
    if quote is None:
        return ActionRecord(
            kind="watchlist", status="rejected", ticker=symbol, action="add",
            code="no_price", detail=f"No market data is available for {symbol}.",
        )

    added = await db.run(lambda conn: db.add_watchlist(conn, symbol, user_id))
    return ActionRecord(
        kind="watchlist", status="executed" if added else "skipped", ticker=symbol,
        action="add",
        detail=(f"Added {symbol} to the watchlist at ${quote.price:,.2f}." if added
                else f"{symbol} was already on the watchlist."),
    )
