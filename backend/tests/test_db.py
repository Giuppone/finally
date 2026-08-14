"""Schema init, seeding and the tracked-set query."""

from __future__ import annotations

import pytest

from app import db
from app.market import SEED_WATCHLIST
from app.schema import STARTING_CASH


@pytest.mark.asyncio
async def test_init_creates_the_schema_and_seeds(temp_db) -> None:
    stats = await db.run(db.stats)
    assert stats["ready"] is True
    assert stats["missing_tables"] == []

    assert await db.run(db.cash_balance) == STARTING_CASH
    rows = await db.run(db.watchlist)
    assert {row["ticker"] for row in rows} == set(SEED_WATCHLIST)
    assert await db.run(db.positions) == []


@pytest.mark.asyncio
async def test_init_is_idempotent(temp_db) -> None:
    await db.initialize()
    await db.initialize()
    rows = await db.run(db.watchlist)
    assert len(rows) == len(SEED_WATCHLIST)          # not 20, not 30


@pytest.mark.asyncio
async def test_an_emptied_watchlist_is_not_reseeded(temp_db) -> None:
    for ticker in SEED_WATCHLIST:
        await db.run(lambda conn, t=ticker: db.remove_watchlist(conn, t))
    await db.initialize()
    assert await db.run(db.watchlist) == []          # the user's choice survives a restart


@pytest.mark.asyncio
async def test_tracked_tickers_is_the_union_of_watchlist_and_positions(temp_db) -> None:
    def setup(conn):
        for ticker in SEED_WATCHLIST:
            db.remove_watchlist(conn, ticker)
        db.add_watchlist(conn, "MU")
        conn.execute(
            "INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at) "
            "VALUES ('p1', 'default', 'AMD', 3.0, 400.0, '2026-08-14T00:00:00Z')"
        )
        conn.commit()

    await db.run(setup)
    assert await db.run(db.tracked_tickers) == {"MU", "AMD"}


@pytest.mark.asyncio
async def test_a_closed_position_leaves_the_tracked_set(temp_db) -> None:
    def setup(conn):
        for ticker in SEED_WATCHLIST:
            db.remove_watchlist(conn, ticker)
        conn.execute(
            "INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at) "
            "VALUES ('p1', 'default', 'AMD', 0.0, 400.0, '2026-08-14T00:00:00Z')"
        )
        conn.commit()

    await db.run(setup)
    assert await db.run(db.tracked_tickers) == set()   # quantity 0 is not an open position


@pytest.mark.asyncio
async def test_watchlist_add_and_remove_report_whether_they_changed_anything(temp_db) -> None:
    assert await db.run(lambda conn: db.add_watchlist(conn, "PYPL")) is True
    assert await db.run(lambda conn: db.add_watchlist(conn, "PYPL")) is False
    assert await db.run(lambda conn: db.remove_watchlist(conn, "PYPL")) is True
    assert await db.run(lambda conn: db.remove_watchlist(conn, "PYPL")) is False


@pytest.mark.asyncio
async def test_ticker_columns_are_case_insensitive(temp_db) -> None:
    """Review.md B1's backstop: even if a boundary forgets normalize_ticker, the UNIQUE
    constraint must not admit `aapl` and `AAPL` as two holdings in the same stock."""
    await db.run(lambda conn: db.add_watchlist(conn, "PYPL"))
    assert await db.run(lambda conn: db.add_watchlist(conn, "pypl")) is False


@pytest.mark.asyncio
async def test_snapshots_are_returned_oldest_first_and_bounded(temp_db) -> None:
    def setup(conn):
        for i in range(10):
            conn.execute(
                "INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at) "
                "VALUES (?, 'default', ?, ?)",
                (f"s{i}", 10000.0 + i, f"2026-08-14T00:00:{i:02d}Z"),
            )
        conn.commit()

    await db.run(setup)
    rows = await db.run(lambda conn: db.snapshots(conn, limit=3))
    assert [r["recorded_at"] for r in rows] == [
        "2026-08-14T00:00:07Z", "2026-08-14T00:00:08Z", "2026-08-14T00:00:09Z"
    ]

    since = await db.run(lambda conn: db.snapshots(conn, since="2026-08-14T00:00:08Z"))
    assert len(since) == 2


@pytest.mark.asyncio
async def test_reset_restores_a_fresh_account(temp_db) -> None:
    def dirty(conn):
        conn.execute("UPDATE users_profile SET cash_balance = 1.0 WHERE id = 'default'")
        conn.execute(
            "INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at) "
            "VALUES ('p1', 'default', 'AMD', 3.0, 400.0, '2026-08-14T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at) "
            "VALUES ('s1', 'default', 1.0, '2026-08-14T00:00:00Z')"
        )
        db.remove_watchlist(conn, "MU")
        conn.commit()

    await db.run(dirty)
    await db.run(db.reset)

    assert await db.run(db.cash_balance) == STARTING_CASH
    assert await db.run(db.positions) == []
    assert await db.run(lambda conn: db.snapshots(conn)) == []
    rows = await db.run(db.watchlist)
    assert {row["ticker"] for row in rows} == set(SEED_WATCHLIST)
