"""The watchlist and portfolio HTTP surface (PLAN.md §8)."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app import db, portfolio, routes
from app.market import SEED_WATCHLIST, Tick
from app.market import router as market_router
from app.schema import STARTING_CASH


@pytest_asyncio.fixture
async def client(temp_db, priced_service):
    app = FastAPI()
    app.include_router(market_router)
    app.include_router(routes.router)
    app.state.market = priced_service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


# ---- watchlist ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_watchlist_merges_live_quotes(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/watchlist")).json()
    entries = {e["ticker"]: e for e in body["tickers"]}
    assert set(entries) == set(SEED_WATCHLIST)
    assert body["mode"] == "simulated"

    assert entries["MU"]["priced"] is True
    assert entries["MU"]["price"] == 100.0
    assert entries["MU"]["direction"] == "flat"
    # PLTR is watched but has no quote yet: null, never $0.00, which would render -100%.
    assert entries["PLTR"]["priced"] is False
    assert "price" not in entries["PLTR"]


@pytest.mark.asyncio
async def test_add_to_watchlist_returns_a_priced_row(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/watchlist", json={"ticker": " pypl "})
    assert response.status_code == 201
    body = response.json()
    assert body["ticker"] == "PYPL" and body["added"] is True
    assert body["price"] > 0                             # priced before it returned (D10)


@pytest.mark.asyncio
async def test_adding_a_watched_ticker_is_not_an_error(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/watchlist", json={"ticker": "MU"})
    assert response.status_code == 201
    assert response.json()["added"] is False


@pytest.mark.asyncio
async def test_add_rejects_a_malformed_ticker(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/watchlist", json={"ticker": "not a ticker"})).status_code == 400
    assert (await client.post("/api/watchlist", json={"ticker": ""})).status_code == 422


@pytest.mark.asyncio
async def test_remove_from_watchlist(client: httpx.AsyncClient) -> None:
    assert (await client.delete("/api/watchlist/mu")).status_code == 204
    body = (await client.get("/api/watchlist")).json()
    assert "MU" not in {e["ticker"] for e in body["tickers"]}


@pytest.mark.asyncio
async def test_removing_a_held_ticker_keeps_it_tracked(
    client: httpx.AsyncClient, priced_service
) -> None:
    """PLAN.md §13 item 4, end to end: the position survives the watchlist removal, so its
    price must keep updating or portfolio value silently goes stale."""
    await client.post("/api/portfolio/trade",
                      json={"ticker": "MU", "quantity": 5, "side": "buy"})
    await client.delete("/api/watchlist/MU")

    assert "MU" in priced_service.tracked
    assert priced_service.quote("MU") is not None
    assert (await client.get("/api/portfolio")).json()["positions"][0]["priced"] is True


@pytest.mark.asyncio
async def test_removing_an_unheld_ticker_evicts_it(
    client: httpx.AsyncClient, priced_service
) -> None:
    await client.delete("/api/watchlist/SLV")
    assert "SLV" not in priced_service.tracked
    assert priced_service.quote("SLV") is None


# ---- portfolio ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_portfolio_on_a_fresh_account(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/portfolio")).json()
    assert body["cash_balance"] == 10000.0
    assert body["positions"] == []
    assert body["total_value"] == 10000.0


@pytest.mark.asyncio
async def test_trade_returns_the_fill_and_the_new_portfolio(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/portfolio/trade",
                                 json={"ticker": "mu", "quantity": 10, "side": "buy"})
    assert response.status_code == 200
    body = response.json()
    assert body["trade"]["status"] == "filled"
    assert body["trade"]["ticker"] == "MU"
    assert body["trade"]["fill_price"] == 100.0
    assert body["trade"]["total"] == 1000.0
    assert body["portfolio"]["cash_balance"] == 9000.0
    assert body["portfolio"]["positions"][0]["quantity"] == 10.0


@pytest.mark.asyncio
async def test_a_rejected_trade_is_a_400_with_a_machine_readable_code(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/portfolio/trade",
                                 json={"ticker": "MU", "quantity": 500, "side": "buy"})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["status"] == "rejected"
    assert detail["code"] == "insufficient_cash"
    assert "insufficient cash" in detail["reason"]


@pytest.mark.asyncio
async def test_trade_rejects_an_unknown_side_before_it_reaches_the_db(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/portfolio/trade",
                                 json={"ticker": "MU", "quantity": 1, "side": "short"})
    assert response.status_code == 422          # the pydantic pattern catches it


@pytest.mark.asyncio
async def test_sell_flow(client: httpx.AsyncClient, priced_service) -> None:
    await client.post("/api/portfolio/trade",
                      json={"ticker": "MU", "quantity": 10, "side": "buy"})
    priced_service.cache.apply(Tick("MU", 120.0, ts=1.0))
    response = await client.post("/api/portfolio/trade",
                                 json={"ticker": "MU", "quantity": 10, "side": "sell"})
    body = response.json()
    assert body["trade"]["total"] == 1200.0
    assert body["portfolio"]["positions"] == []
    assert body["portfolio"]["cash_balance"] == 10200.0
    assert body["portfolio"]["total_return"] == 200.0


@pytest.mark.asyncio
async def test_portfolio_history(client: httpx.AsyncClient, priced_service) -> None:
    await client.post("/api/portfolio/trade",
                      json={"ticker": "MU", "quantity": 10, "side": "buy"})
    priced_service.cache.apply(Tick("MU", 110.0, ts=1.0))
    await client.post("/api/portfolio/trade",
                      json={"ticker": "MU", "quantity": 1, "side": "buy"})

    body = (await client.get("/api/portfolio/history")).json()
    assert len(body["points"]) == 2
    assert body["starting_cash"] == 10000.0
    assert set(body["points"][0]) == {"recorded_at", "total_value"}
    assert body["points"][0]["recorded_at"] <= body["points"][1]["recorded_at"]


@pytest.mark.asyncio
async def test_portfolio_history_limit_is_bounded(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/portfolio/history?limit=99999")).status_code == 422
    assert (await client.get("/api/portfolio/history?limit=0")).status_code == 422


@pytest.mark.asyncio
async def test_reset_restores_a_fresh_account(client: httpx.AsyncClient) -> None:
    await client.post("/api/portfolio/trade",
                      json={"ticker": "MU", "quantity": 10, "side": "buy"})
    await client.delete("/api/watchlist/AMD")

    body = (await client.post("/api/portfolio/reset")).json()
    assert body["cash_balance"] == 10000.0
    assert body["positions"] == []
    assert (await client.get("/api/portfolio/history")).json()["points"] == []
    watched = {e["ticker"] for e in (await client.get("/api/watchlist")).json()["tickers"]}
    assert watched == set(SEED_WATCHLIST)


@pytest.mark.asyncio
async def test_reset_resyncs_the_tracked_set(
    client: httpx.AsyncClient, priced_service
) -> None:
    await client.post("/api/portfolio/reset")
    tracked = await db.run(db.tracked_tickers)
    assert tracked == set(SEED_WATCHLIST)
    assert priced_service.tracked == tracked


# ---- reset serialisation (Back_end_review.md P1) ----------------------------

@pytest.mark.asyncio
async def test_reset_waits_for_an_in_flight_trade(client: httpx.AsyncClient) -> None:
    """`execute_trade` takes trade_lock(); a lock only one side takes serialises nothing.

    Before the fix, reset ran straight through while a trade sat inside the lock holding a
    price it was about to commit — so the trade's write could land on top of the cleared
    tables, leaving a position and a cash balance != $10,000 in what reset just reported
    as a fresh account. Holding the lock here stands in for that in-flight trade.
    """
    async with portfolio.trade_lock():
        pending = asyncio.create_task(client.post("/api/portfolio/reset"))
        await asyncio.sleep(0.05)
        assert not pending.done(), "reset ran while a trade held the lock"

    response = await pending
    assert response.status_code == 200
    assert response.json()["cash_balance"] == STARTING_CASH


# ---- portfolio sessions (save / load) ---------------------------------------

@pytest.mark.asyncio
async def test_session_export_carries_cash_positions_and_watchlist(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/api/portfolio/trade",
                      json={"ticker": "MU", "side": "buy", "quantity": 10})

    document = (await client.get("/api/session")).json()
    assert document["version"] == 1
    assert document["cash_balance"] == STARTING_CASH - 10 * 100.0
    assert document["positions"] == [{"ticker": "MU", "quantity": 10.0, "avg_cost": 100.0}]
    assert set(document["watchlist"]) == set(SEED_WATCHLIST)
    assert document["meta"]["mode"] == "simulated"


@pytest.mark.asyncio
async def test_session_round_trip_restores_the_exact_account(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/api/portfolio/trade",
                      json={"ticker": "MU", "side": "buy", "quantity": 10})
    await client.post("/api/portfolio/trade",
                      json={"ticker": "AMD", "side": "buy", "quantity": 4})
    await client.delete("/api/watchlist/SLV")
    saved = (await client.get("/api/session")).json()

    await client.post("/api/portfolio/reset")
    assert (await client.get("/api/portfolio")).json()["cash_balance"] == STARTING_CASH

    response = await client.post("/api/session", json=saved)
    assert response.status_code == 200
    assert response.json()["loaded"] == {"positions": 2, "watchlist": 9}

    restored = (await client.get("/api/session")).json()
    assert restored["cash_balance"] == saved["cash_balance"]
    assert restored["positions"] == saved["positions"]
    assert restored["watchlist"] == saved["watchlist"]


@pytest.mark.asyncio
async def test_session_load_preserves_avg_cost_when_the_price_has_moved(
    client: httpx.AsyncClient, priced_service
) -> None:
    """The reason this is an endpoint and not a replay of trades: buying the position back
    would fill at today's price and silently rewrite the cost basis — so every unrealised
    P&L number in the restored account would be wrong."""
    await client.post("/api/portfolio/trade",
                      json={"ticker": "MU", "side": "buy", "quantity": 10})
    saved = (await client.get("/api/session")).json()

    priced_service._cache.apply(Tick("MU", 150.0, 0))
    await client.post("/api/session", json=saved)

    holding = (await client.get("/api/portfolio")).json()["positions"][0]
    assert holding["avg_cost"] == 100.0            # saved cost, not the 150 fill
    assert holding["price"] == 150.0
    assert holding["unrealized_pnl"] == 500.0


@pytest.mark.asyncio
async def test_session_load_resyncs_the_tracked_set(
    client: httpx.AsyncClient, priced_service
) -> None:
    saved = {"version": 1, "cash_balance": 5000.0,
             "positions": [{"ticker": "MU", "quantity": 1, "avg_cost": 90.0}],
             "watchlist": ["AMD"]}
    await client.post("/api/session", json=saved)

    # watchlist ∪ open positions (PLAN.md §6) — a restored position whose ticker is not
    # watched must still tick, or its value silently freezes.
    assert priced_service.tracked == {"AMD", "MU"}
    assert await db.run(db.tracked_tickers) == {"AMD", "MU"}


@pytest.mark.asyncio
async def test_session_load_snapshots_so_the_pnl_chart_is_not_blank(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/api/session", json={
        "version": 1, "cash_balance": 5000.0,
        "positions": [{"ticker": "MU", "quantity": 10, "avg_cost": 90.0}],
        "watchlist": ["MU"],
    })
    points = (await client.get("/api/portfolio/history")).json()["points"]
    assert [p["total_value"] for p in points] == [6000.0]


@pytest.mark.asyncio
async def test_session_load_keeps_chat_history(client: httpx.AsyncClient) -> None:
    """A load restores a portfolio. Deleting the conversation is `reset`'s job."""
    await db.run(lambda conn: db.add_chat_message(conn, "user", "hello"))
    await client.post("/api/session", json={
        "version": 1, "cash_balance": 1.0, "positions": [], "watchlist": [],
    })
    assert len(await db.run(db.chat_messages)) == 1


@pytest.mark.asyncio
async def test_session_load_normalizes_tickers(client: httpx.AsyncClient) -> None:
    await client.post("/api/session", json={
        "version": 1, "cash_balance": 0.0,
        "positions": [{"ticker": " mu ", "quantity": 2, "avg_cost": 10.0}],
        "watchlist": ["amd"],
    })
    document = (await client.get("/api/session")).json()
    assert document["positions"][0]["ticker"] == "MU"
    assert document["watchlist"] == ["AMD"]


@pytest.mark.asyncio
async def test_session_load_rejects_a_duplicate_position(
    client: httpx.AsyncClient,
) -> None:
    """Merging them would invent an average cost the user never held."""
    response = await client.post("/api/session", json={
        "version": 1, "cash_balance": 0.0, "watchlist": [],
        "positions": [{"ticker": "MU", "quantity": 1, "avg_cost": 10.0},
                      {"ticker": "mu", "quantity": 2, "avg_cost": 20.0}],
    })
    assert response.status_code == 400
    assert "duplicate" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_session_load_rejects_an_unknown_version(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/session", json={
        "version": 99, "cash_balance": 0.0, "positions": [], "watchlist": [],
    })
    assert response.status_code == 400
    assert "version" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"cash_balance": -1.0, "positions": [], "watchlist": []},
    {"cash_balance": 0.0, "watchlist": [],
     "positions": [{"ticker": "MU", "quantity": 0, "avg_cost": 10.0}]},
    {"cash_balance": 0.0, "watchlist": [],
     "positions": [{"ticker": "MU", "quantity": -5, "avg_cost": 10.0}]},
])
async def test_session_load_rejects_impossible_documents(
    client: httpx.AsyncClient, bad: dict
) -> None:
    """A zero-quantity row is the phantom position a full sell deletes (Review.md B11);
    restoring one would put it straight back."""
    assert (await client.post("/api/session", json=bad)).status_code == 422


@pytest.mark.asyncio
async def test_session_load_waits_for_an_in_flight_trade(
    client: httpx.AsyncClient,
) -> None:
    """Same hazard as reset (Back_end_review.md P1): a trade already past its price read
    would otherwise commit on top of the just-restored tables."""
    async with portfolio.trade_lock():
        pending = asyncio.create_task(client.post("/api/session", json={
            "version": 1, "cash_balance": 42.0, "positions": [], "watchlist": [],
        }))
        await asyncio.sleep(0.05)
        assert not pending.done(), "session load ran while a trade held the lock"

    assert (await pending).status_code == 200
    assert (await client.get("/api/portfolio")).json()["cash_balance"] == 42.0
