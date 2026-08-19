"""The /api/history surface: shapes, range filtering, and degrading without a ledger."""

from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app import routes
from app.history import bars as bars_module
from app.history import ledger as ledger_module
from app.history.routes import router as history_router
from app.routes import SessionDocument


@pytest_asyncio.fixture
async def client(temp_db, priced_service):
    # `routes.router` rides along because the round-trip test POSTs the reconstructed
    # document back through /api/session - the endpoint the loader actually uses.
    app = FastAPI()
    app.include_router(history_router)
    app.include_router(routes.router)
    app.state.market = priced_service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture(autouse=True)
def _fresh_memo():
    """The reconstruction is memoised on app.state keyed by file identity, and the bars cache
    keys on a path that a monkeypatched test may repoint at a different file."""
    bars_module.reset_cache()
    yield
    bars_module.reset_cache()


async def _available(client) -> bool:
    return (await client.get("/api/history/portfolio")).json()["available"]


@pytest.mark.asyncio
async def test_portfolio_curve_shape(client):
    body = (await client.get("/api/history/portfolio?range=max")).json()
    if not body["available"]:
        pytest.skip("no committed ledger.json")

    assert body["currency"] == "USD"
    assert body["range"] == "max"
    assert len(body["points"]) >= 2
    first, last = body["points"][0], body["points"][-1]
    for key in ("date", "ts", "total_value", "return_pct", "positions_value",
                "carry_value", "cash_balance"):
        assert key in first
    # ISO date plus epoch-ms, so one chart accessor reads this and the live ring buffer.
    assert first["date"][:2] == "20" and isinstance(first["ts"], int)
    assert first["return_pct"] == 0.0
    assert body["base_value"] == pytest.approx(first["total_value"])
    assert last["ts"] > first["ts"]
    assert body["meta"]["fx_observations"] >= 1


@pytest.mark.asyncio
async def test_return_pct_rebases_to_the_filtered_window(client):
    """The percentage is computed server-side precisely because it depends on the window.

    A client rebasing `max` data to draw `3m` would show the wrong number, and one refetching
    per toggle would flicker. Both are avoided by shipping it already correct.
    """
    if not await _available(client):
        pytest.skip("no committed ledger.json")

    full = (await client.get("/api/history/portfolio?range=max")).json()
    short = (await client.get("/api/history/portfolio?range=3m")).json()
    assert len(short["points"]) < len(full["points"])
    assert short["points"][0]["return_pct"] == 0.0
    assert short["base_value"] == pytest.approx(short["points"][0]["total_value"])
    # Same final dollar value, different percentage - which is the whole point.
    assert short["points"][-1]["total_value"] == pytest.approx(
        full["points"][-1]["total_value"])


@pytest.mark.asyncio
async def test_unknown_range_falls_back_rather_than_422ing(client):
    body = (await client.get("/api/history/portfolio?range=nonsense")).json()
    assert body["range"] == "max"


@pytest.mark.asyncio
async def test_ranges_advertised_in_meta_exclude_1y(client):
    """The bars cache starts in December, so a "1y" option would return eight months and
    call it a year."""
    meta = (await client.get("/api/history/portfolio")).json()["meta"]
    assert "1y" not in meta["ranges"]
    assert meta["ranges"] == ["1m", "3m", "6m", "ytd", "max"]


@pytest.mark.asyncio
async def test_daily_prices_single_and_bulk(client):
    bars = bars_module.load()
    if not bars.tickers():
        pytest.skip("no bars cache")
    ticker = bars.tickers()[0]

    one = await client.get(f"/api/history/prices/{ticker.lower()}?range=3m")
    assert one.status_code == 200
    body = one.json()
    assert body["ticker"] == ticker
    assert body["points"] and set(body["points"][0]) == {"date", "ts", "close"}

    bulk = (await client.get(f"/api/history/prices?tickers={ticker},NOPE")).json()
    assert bulk["series"][ticker]
    # A stale symbol must not fail the batch - same contract as /api/prices/history.
    assert bulk["series"]["NOPE"] == []
    assert bulk["unknown"] == ["NOPE"]


@pytest.mark.asyncio
async def test_single_daily_route_404s_on_an_unknown_ticker(client):
    """Unlike the bulk form: a single-resource route returning an empty body hides a typo."""
    response = await client.get("/api/history/prices/NOSUCHTICKER")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "no_daily_history"


@pytest.mark.asyncio
async def test_bulk_daily_rejects_an_empty_ticker_list(client):
    assert (await client.get("/api/history/prices?tickers=,,")).status_code == 400


@pytest.mark.asyncio
async def test_session_document_is_acceptable_to_post_session(client):
    """The contract that matters: whatever this emits, /api/session must take.

    Three validators on the far side would otherwise 422 - cash_balance ge=0,
    quantity gt=0, and the letters-only ticker normalisation.
    """
    response = await client.get("/api/history/session")
    if response.status_code == 404:
        pytest.skip("no committed ledger.json")

    payload = response.json()
    document = SessionDocument(**payload["session"])
    assert document.positions
    assert document.cash_balance >= 0
    assert all(position.quantity > 0 for position in document.positions)
    assert all(position.ticker.isalpha() and len(position.ticker) <= 5
               for position in document.positions)
    assert set(document.watchlist) == {p.ticker for p in document.positions}


@pytest.mark.asyncio
async def test_session_round_trips_through_post_session(client):
    """End to end: reconstruct, load, and read the same quantities back out."""
    response = await client.get("/api/history/session")
    if response.status_code == 404:
        pytest.skip("no committed ledger.json")
    document = response.json()["session"]

    loaded = await client.post("/api/session", json=document)
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["loaded"]["positions"] == len(document["positions"])

    restored = (await client.get("/api/session")).json()
    by_ticker = {p["ticker"]: p for p in restored["positions"]}
    for position in document["positions"]:
        assert by_ticker[position["ticker"]]["quantity"] == pytest.approx(
            position["quantity"])
        # The cost basis is the reason this goes through /api/session rather than market buys.
        assert by_ticker[position["ticker"]]["avg_cost"] == pytest.approx(
            position["avg_cost"])


@pytest.mark.asyncio
async def test_daily_prices_carry_the_users_trades_and_held_since(client):
    """The misreading that prompted the markers: a chart showing market price for the whole
    bars window needs the user's entry marked, or a recent buy reads as a long holding."""
    if not await _available(client):
        pytest.skip("no committed ledger.json")

    body = (await client.get("/api/history/prices/MP?range=max")).json()
    assert body["held_since"] is not None
    assert len(body["trades"]) >= 1
    trade = body["trades"][0]
    assert set(trade) >= {"date", "ts", "side", "shares", "price", "usd"}
    assert trade["side"] in ("buy", "sell")
    # Every reported trade sits inside the returned window.
    assert body["start_date"] <= trade["date"] <= body["end_date"]

    # A ticker in the bars cache that the ledger never touched (ANET is watchlist-only)
    # reports cleanly, not nulls that crash the chart.
    quiet = (await client.get("/api/history/prices/ANET?range=max")).json()
    assert quiet["trades"] == []
    assert quiet["held_since"] is None


# ---- degrading without a ledger --------------------------------------------------

@pytest.mark.asyncio
async def test_missing_ledger_is_200_available_false(client, monkeypatch, tmp_path):
    """Not a 404. The frontend fetches this on every page load, so a stock deployment with no
    ledger would log one on each - and the panel needs to render an explanation, not an error.
    """
    monkeypatch.setattr(ledger_module, "default_path", lambda: tmp_path / "absent.json")
    body = (await client.get("/api/history/portfolio")).json()
    assert body["available"] is False
    assert body["points"] == []
    assert any("import_broker_with_dates" in w for w in body["warnings"])


@pytest.mark.asyncio
async def test_missing_ledger_404s_the_session_route(client, monkeypatch, tmp_path):
    """The exception: this caller asked for one specific thing to load, and there is none."""
    monkeypatch.setattr(ledger_module, "default_path", lambda: tmp_path / "absent.json")
    response = await client.get("/api/history/session")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "no_ledger"


@pytest.mark.asyncio
async def test_missing_bars_degrades_rather_than_500ing(client, monkeypatch, tmp_path):
    monkeypatch.setenv("FINALLY_BARS_PATH", str(tmp_path / "absent.json"))
    bars_module.reset_cache()
    body = (await client.get("/api/history/portfolio")).json()
    assert body["available"] is False
    assert (await client.get("/api/history/prices/AAPL")).status_code == 404


@pytest.mark.asyncio
async def test_malformed_bars_file_degrades(client, monkeypatch, tmp_path):
    broken = tmp_path / "bars.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("FINALLY_BARS_PATH", str(broken))
    bars_module.reset_cache()
    assert (await client.get("/api/history/portfolio")).json()["available"] is False


@pytest.mark.asyncio
async def test_malformed_ledger_surfaces_rather_than_booting_broken(client, monkeypatch, tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"version": 99}), encoding="utf-8")
    monkeypatch.setattr(ledger_module, "default_path", lambda: path)
    with pytest.raises(ledger_module.LedgerError):
        (await client.get("/api/history/portfolio"))
