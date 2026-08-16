"""Trade execution, valuation and snapshots — Review.md B11/B12/B13/B16, C3."""

from __future__ import annotations

import asyncio

import pytest

from app import db, portfolio
from app.market import Tick
from app.portfolio import execute_trade, validate_quantity, value_portfolio
from app.schema import STARTING_CASH


async def _state(service):
    return await db.run(lambda conn: value_portfolio(conn, service))


# ---- quantity validation (B12) ----------------------------------------------

@pytest.mark.parametrize("raw", [0, -1, -0.5, float("nan"), float("inf"), 1e12, "abc", None])
def test_validate_quantity_rejects(raw) -> None:
    quantity, error = validate_quantity(raw)
    assert quantity is None and error


@pytest.mark.parametrize("raw,expected", [
    (1, 1.0), (0.5, 0.5), ("2.5", 2.5), (1.23456789, 1.234568),   # 6dp
])
def test_validate_quantity_accepts(raw, expected) -> None:
    quantity, error = validate_quantity(raw)
    assert error is None and quantity == expected


# ---- buys --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_buy_debits_cash_and_opens_a_position(temp_db, priced_service) -> None:
    result = await execute_trade("mu", "buy", 10, priced_service)
    assert result.filled
    assert result.fill_price == 100.0
    assert result.total == 1000.0
    assert result.cash_balance == STARTING_CASH - 1000.0

    state = await _state(priced_service)
    assert state["cash_balance"] == 9000.0
    assert len(state["positions"]) == 1
    holding = state["positions"][0]
    assert (holding["ticker"], holding["quantity"], holding["avg_cost"]) == ("MU", 10.0, 100.0)
    assert state["total_value"] == 10000.0                 # nothing created or destroyed


@pytest.mark.asyncio
async def test_a_second_buy_re_averages_the_cost(temp_db, priced_service) -> None:
    await execute_trade("MU", "buy", 10, priced_service)          # 10 @ 100
    priced_service.cache.apply(Tick("MU", 200.0, ts=1.0))
    await execute_trade("MU", "buy", 10, priced_service)          # 10 @ 200

    holding = (await _state(priced_service))["positions"][0]
    assert holding["quantity"] == 20.0
    assert holding["avg_cost"] == 150.0


@pytest.mark.asyncio
async def test_buy_rejects_insufficient_cash(temp_db, priced_service) -> None:
    result = await execute_trade("MU", "buy", 200, priced_service)   # $20k of a $10k account
    assert not result.filled
    assert result.code == "insufficient_cash"
    assert "insufficient cash" in result.reason
    assert await db.run(db.cash_balance) == STARTING_CASH            # untouched


@pytest.mark.asyncio
async def test_a_buy_costing_exactly_the_balance_is_allowed(temp_db, priced_service) -> None:
    """Floating-point equality would otherwise reject the one trade a user is most likely
    to try deliberately (Review.md B12)."""
    result = await execute_trade("MU", "buy", 100, priced_service)   # 100 x $100 = $10,000
    assert result.filled
    assert (await _state(priced_service))["cash_balance"] == 0.0


@pytest.mark.asyncio
async def test_buy_auto_adds_the_ticker_to_the_watchlist(temp_db, priced_service) -> None:
    await db.run(lambda conn: db.remove_watchlist(conn, "MU"))
    result = await execute_trade("MU", "buy", 1, priced_service)
    assert result.watchlist_added is True
    rows = await db.run(db.watchlist)
    assert "MU" in {row["ticker"] for row in rows}


# ---- sells -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_partial_sell_credits_cash_and_leaves_avg_cost_alone(temp_db, priced_service) -> None:
    await execute_trade("MU", "buy", 10, priced_service)             # 10 @ 100
    priced_service.cache.apply(Tick("MU", 150.0, ts=1.0))
    result = await execute_trade("MU", "sell", 4, priced_service)     # 4 @ 150

    assert result.filled and result.total == 600.0
    state = await _state(priced_service)
    assert state["cash_balance"] == 9000.0 + 600.0
    holding = state["positions"][0]
    assert holding["quantity"] == 6.0
    assert holding["avg_cost"] == 100.0                              # only buys re-average
    assert holding["unrealized_pnl"] == 300.0                        # 6 x (150 - 100)


@pytest.mark.asyncio
async def test_selling_everything_deletes_the_row(temp_db, priced_service) -> None:
    await execute_trade("MU", "buy", 10, priced_service)
    await execute_trade("MU", "sell", 10, priced_service)
    assert await db.run(db.positions) == []


@pytest.mark.asyncio
async def test_selling_a_position_built_in_pieces_leaves_no_dust(temp_db, priced_service) -> None:
    """0.1 + 0.2 - 0.3 is not 0 in binary floating point: without the dust threshold the
    row survives with ~4e-16 shares, renders in the positions table and pins the ticker in
    the cache forever (Review.md B11)."""
    await execute_trade("MU", "buy", 0.1, priced_service)
    await execute_trade("MU", "buy", 0.2, priced_service)
    result = await execute_trade("MU", "sell", 0.3, priced_service)
    assert result.filled
    assert await db.run(db.positions) == []


@pytest.mark.asyncio
async def test_sell_rejects_more_shares_than_held(temp_db, priced_service) -> None:
    await execute_trade("MU", "buy", 5, priced_service)
    result = await execute_trade("MU", "sell", 6, priced_service)
    assert not result.filled and result.code == "insufficient_shares"
    assert (await _state(priced_service))["positions"][0]["quantity"] == 5.0


@pytest.mark.asyncio
async def test_sell_rejects_a_ticker_never_held(temp_db, priced_service) -> None:
    result = await execute_trade("SLV", "sell", 1, priced_service)
    assert not result.filled and result.code == "insufficient_shares"


# ---- rejections --------------------------------------------------------------

@pytest.mark.asyncio
async def test_rejects_a_malformed_ticker(temp_db, priced_service) -> None:
    result = await execute_trade("not a ticker", "buy", 1, priced_service)
    assert not result.filled and result.code == "invalid_ticker"


@pytest.mark.asyncio
async def test_rejects_an_unknown_side(temp_db, priced_service) -> None:
    result = await execute_trade("MU", "short", 1, priced_service)
    assert not result.filled and result.code == "invalid_side"


@pytest.mark.asyncio
async def test_rejects_a_bad_quantity(temp_db, priced_service) -> None:
    result = await execute_trade("MU", "buy", -5, priced_service)
    assert not result.filled and result.code == "invalid_quantity"
    assert await db.run(db.cash_balance) == STARTING_CASH


@pytest.mark.asyncio
async def test_a_rejected_trade_writes_nothing(temp_db, priced_service) -> None:
    await execute_trade("MU", "buy", 500, priced_service)
    trades = await db.run(lambda conn: conn.execute("SELECT * FROM trades").fetchall())
    assert trades == []


# ---- concurrency (B13) -------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_buys_cannot_drive_cash_negative(temp_db, priced_service) -> None:
    """Async handlers interleave at every await, so without the trade lock two
    individually-affordable buys both pass validation and overspend the account."""
    results = await asyncio.gather(
        *[execute_trade("MU", "buy", 40, priced_service) for _ in range(5)]   # $4k each
    )
    filled = [r for r in results if r.filled]
    assert len(filled) == 2                                  # 8k of a 10k account
    state = await _state(priced_service)
    assert state["cash_balance"] == 2000.0
    assert state["positions"][0]["quantity"] == 80.0


@pytest.mark.asyncio
async def test_concurrent_sells_cannot_oversell(temp_db, priced_service) -> None:
    await execute_trade("MU", "buy", 10, priced_service)
    results = await asyncio.gather(
        *[execute_trade("MU", "sell", 6, priced_service) for _ in range(3)]
    )
    assert len([r for r in results if r.filled]) == 1
    assert (await _state(priced_service))["positions"][0]["quantity"] == 4.0


# ---- valuation (B16 / D6) ----------------------------------------------------

@pytest.mark.asyncio
async def test_an_unpriced_position_falls_back_to_avg_cost(temp_db, priced_service) -> None:
    await execute_trade("MU", "buy", 10, priced_service)
    priced_service.cache.evict("MU")                         # simulates a fresh restart

    state = await _state(priced_service)
    holding = state["positions"][0]
    assert holding["priced"] is False
    assert holding["price"] == 100.0                         # avg_cost, not 0
    assert holding["unrealized_pnl"] == 0.0                  # not -100%
    assert state["all_priced"] is False
    assert state["total_value"] == 10000.0


@pytest.mark.asyncio
async def test_weights_and_total_return(temp_db, priced_service) -> None:
    await execute_trade("MU", "buy", 10, priced_service)      # $1,000
    await execute_trade("AMD", "buy", 20, priced_service)     # $1,000
    priced_service.cache.apply(Tick("MU", 300.0, ts=1.0))     # MU -> $3,000

    state = await _state(priced_service)
    weights = {h["ticker"]: h["weight"] for h in state["positions"]}
    assert weights["MU"] == pytest.approx(0.75)
    assert weights["AMD"] == pytest.approx(0.25)
    assert state["total_return"] == 2000.0
    assert state["total_return_pct"] == 20.0
    assert state["starting_cash"] == STARTING_CASH


@pytest.mark.asyncio
async def test_an_empty_portfolio_values_cleanly(temp_db, priced_service) -> None:
    state = await _state(priced_service)
    assert state == {
        "cash_balance": 10000.0, "positions": [], "positions_value": 0.0,
        "total_value": 10000.0, "unrealized_pnl": 0.0, "starting_cash": 10000.0,
        "total_return": 0.0, "total_return_pct": 0.0, "all_priced": True,
    }


# ---- snapshots (B16 / C3) ----------------------------------------------------

@pytest.mark.asyncio
async def test_a_trade_records_a_snapshot(temp_db, priced_service) -> None:
    await execute_trade("MU", "buy", 10, priced_service)
    rows = await db.run(lambda conn: db.snapshots(conn))
    assert len(rows) == 1
    assert rows[0]["total_value"] == 10000.0


@pytest.mark.asyncio
async def test_snapshots_are_skipped_until_every_position_is_priced(
    temp_db, priced_service
) -> None:
    await execute_trade("MU", "buy", 10, priced_service)
    await db.run(lambda conn: conn.execute("DELETE FROM portfolio_snapshots"))
    priced_service.cache.evict("MU")

    assert await portfolio.snapshot_now(priced_service) is False
    assert await db.run(lambda conn: db.snapshots(conn)) == []


@pytest.mark.asyncio
async def test_an_unchanged_portfolio_does_not_accumulate_rows(temp_db, priced_service) -> None:
    await execute_trade("MU", "buy", 10, priced_service)
    for _ in range(5):
        assert await portfolio.snapshot_now(priced_service) is False
    assert len(await db.run(lambda conn: db.snapshots(conn))) == 1

    priced_service.cache.apply(Tick("MU", 101.0, ts=1.0))
    assert await portfolio.snapshot_now(priced_service) is True
    assert len(await db.run(lambda conn: db.snapshots(conn))) == 2


@pytest.mark.asyncio
async def test_snapshot_task_starts_and_stops(temp_db, priced_service) -> None:
    task = portfolio.SnapshotTask(priced_service, interval=0.01)
    task.start()
    await execute_trade("MU", "buy", 10, priced_service)
    await asyncio.sleep(0.05)
    await task.stop()
    await task.stop()                                        # idempotent


# ---- concurrent snapshot writers (Back_end_review.md P2) --------------------

@pytest.mark.asyncio
async def test_concurrent_snapshots_write_exactly_one_row(temp_db, priced_service) -> None:
    """The 30s loop and a trade's post-commit snapshot can land in the same window.

    Each db.run lands on its own asyncio.to_thread worker with its own connection, so as
    three loose statements every writer read the same last_snapshot, judged the value
    changed against that stale read, and inserted — duplicate points on the P&L chart.
    """
    await execute_trade("MU", "buy", 10, priced_service)
    baseline = len(await db.run(lambda conn: db.snapshots(conn)))

    priced_service.cache.apply(Tick("MU", 137.0, ts=2.0))
    results = await asyncio.gather(*(portfolio.snapshot_now(priced_service) for _ in range(8)))

    rows = await db.run(lambda conn: db.snapshots(conn))
    assert len(rows) == baseline + 1
    assert sum(results) == 1              # exactly one caller reports having written
