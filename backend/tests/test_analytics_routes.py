"""The two analytics endpoints and the batch executor behind Apply."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app import analytics, portfolio, routes
from app.market import router as market_router


@pytest_asyncio.fixture
async def client(temp_db, priced_service):
    app = FastAPI()
    app.include_router(market_router)
    app.include_router(routes.router)
    app.include_router(analytics.router)
    app.state.market = priced_service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def buy(client: httpx.AsyncClient, ticker: str, quantity: float) -> None:
    response = await client.post("/api/portfolio/trade",
                                 json={"ticker": ticker, "side": "buy", "quantity": quantity})
    assert response.status_code == 200, response.text


# ---- POST /api/analytics/risk ------------------------------------------------

@pytest.mark.asyncio
async def test_risk_on_explicit_weights(client: httpx.AsyncClient) -> None:
    body = (await client.post("/api/analytics/risk", json={
        "holdings": [{"ticker": "MU", "weight": 0.6}, {"ticker": "SLV", "weight": 0.4}],
    })).json()

    assert body["volatility"] > 0
    assert body["sharpe"] is not None
    assert [row["ticker"] for row in body["positions"]] == ["MU", "SLV"]
    assert sum(row["risk_share"] for row in body["positions"]) == pytest.approx(1.0, abs=1e-4)
    assert body["correlations"]["tickers"] == ["MU", "SLV"]
    assert "damped" in body["expected_return_basis"]


@pytest.mark.asyncio
async def test_risk_defaults_to_the_live_portfolio(client: httpx.AsyncClient) -> None:
    """No holdings supplied means "what I actually own", cash weight and all."""
    await buy(client, "MU", 10)          # $1,000 of a $10,000 book
    await buy(client, "AMD", 20)         # $1,000

    body = (await client.post("/api/analytics/risk", json={})).json()
    assert [row["ticker"] for row in body["positions"]] == ["AMD", "MU"]
    assert body["cash_weight"] == pytest.approx(0.8, abs=1e-3)
    assert body["var_95_1d_parametric"] > 0        # total_value came from the portfolio


@pytest.mark.asyncio
async def test_risk_normalizes_oversized_weights_and_says_so(
    client: httpx.AsyncClient,
) -> None:
    body = (await client.post("/api/analytics/risk", json={
        "holdings": [{"ticker": "MU", "weight": 1.0}, {"ticker": "AMD", "weight": 1.0}],
    })).json()
    assert [row["weight"] for row in body["positions"]] == [0.5, 0.5]
    assert any("normalised" in warning for warning in body["warnings"])


@pytest.mark.asyncio
async def test_risk_flags_an_uncalibrated_ticker(client: httpx.AsyncClient) -> None:
    """A generic sigma=0.45 is a placeholder, not a measurement - say so."""
    body = (await client.post("/api/analytics/risk", json={
        "holdings": [{"ticker": "ZZZZ", "weight": 1.0}],
    })).json()
    assert body["positions"][0]["calibrated"] is False
    assert any("ZZZZ" in warning for warning in body["warnings"])


@pytest.mark.asyncio
async def test_risk_on_an_empty_account_is_a_400_not_a_zero(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/analytics/risk", json={})
    assert response.status_code == 400
    assert "no positions" in response.json()["detail"]


@pytest.mark.asyncio
async def test_risk_rejects_a_duplicate_holding(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/analytics/risk", json={
        "holdings": [{"ticker": "MU", "weight": 0.5}, {"ticker": "mu", "weight": 0.5}],
    })
    assert response.status_code == 400


# ---- POST /api/analytics/rebalance -------------------------------------------

@pytest.mark.asyncio
async def test_rebalance_lowers_volatility(client: httpx.AsyncClient) -> None:
    """The assertion that matters: min-variance is never worse on its own objective."""
    await buy(client, "MU", 80)          # $8,000 - a deliberately lopsided book
    await buy(client, "AMD", 10)         # $500
    await buy(client, "SLV", 20)         # $500

    body = (await client.post("/api/analytics/rebalance",
                              json={"objective": "min_variance"})).json()
    assert body["after"]["volatility"] <= body["before"]["volatility"]
    assert body["trades"], "a 94% single-name book has something to rebalance"
    assert {leg["side"] for leg in body["trades"]} <= {"buy", "sell"}


@pytest.mark.asyncio
async def test_rebalance_sells_before_it_buys(client: httpx.AsyncClient) -> None:
    await buy(client, "MU", 80)
    await buy(client, "AMD", 10)
    await buy(client, "SLV", 20)

    trades = (await client.post("/api/analytics/rebalance",
                                json={"objective": "risk_parity"})).json()["trades"]
    sides = [leg["side"] for leg in trades]
    assert sides.index("sell") < sides.index("buy") if "buy" in sides else True


@pytest.mark.asyncio
async def test_rebalance_respects_the_weight_cap(client: httpx.AsyncClient) -> None:
    await buy(client, "MU", 80)
    await buy(client, "AMD", 10)
    await buy(client, "SLV", 20)

    body = (await client.post("/api/analytics/rebalance", json={
        "objective": "min_variance", "constraints": {"max_weight": 0.4},
    })).json()
    assert all(row["target_weight"] <= 0.4 + 1e-6 for row in body["targets"])


@pytest.mark.asyncio
async def test_rebalance_can_model_tickers_not_yet_held(client: httpx.AsyncClient) -> None:
    """Selecting a name you do not own is the point of the selection UI."""
    await buy(client, "MU", 10)
    body = (await client.post("/api/analytics/rebalance", json={
        "objective": "equal_weight",
        "holdings": [{"ticker": "MU"}, {"ticker": "AMD"}, {"ticker": "SLV"}],
    })).json()
    assert {row["ticker"] for row in body["targets"]} == {"MU", "AMD", "SLV"}
    assert any(leg["ticker"] == "AMD" and leg["side"] == "buy" for leg in body["trades"])


@pytest.mark.asyncio
async def test_rebalance_rejects_an_unknown_objective(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/analytics/rebalance",
                                 json={"objective": "moon_shot"})
    assert response.status_code == 400
    assert "min_variance" in response.json()["detail"]


@pytest.mark.asyncio
async def test_rebalance_needs_two_names(client: httpx.AsyncClient) -> None:
    await buy(client, "MU", 10)
    response = await client.post("/api/analytics/rebalance", json={})
    assert response.status_code == 400
    assert "at least two" in response.json()["detail"]


@pytest.mark.asyncio
async def test_an_impossible_cap_explains_the_arithmetic(client: httpx.AsyncClient) -> None:
    await buy(client, "MU", 10)
    await buy(client, "AMD", 10)
    await buy(client, "SLV", 10)

    response = await client.post("/api/analytics/rebalance", json={
        "objective": "min_variance", "constraints": {"max_weight": 0.2},
    })
    assert response.status_code == 400
    assert "1/3" in response.json()["detail"]


@pytest.mark.asyncio
async def test_rebalance_suggests_but_never_trades(client: httpx.AsyncClient) -> None:
    """A button labelled "suggest" must not move the account."""
    await buy(client, "MU", 80)
    await buy(client, "AMD", 10)
    before = (await client.get("/api/portfolio")).json()

    await client.post("/api/analytics/rebalance", json={"objective": "min_variance"})

    after = (await client.get("/api/portfolio")).json()
    assert after["cash_balance"] == before["cash_balance"]
    assert after["positions"] == before["positions"]


# ---- POST /api/portfolio/rebalance (Apply) -----------------------------------

@pytest.mark.asyncio
async def test_apply_executes_the_suggested_plan(client: httpx.AsyncClient) -> None:
    await buy(client, "MU", 80)
    await buy(client, "AMD", 10)
    await buy(client, "SLV", 20)

    plan = (await client.post("/api/analytics/rebalance",
                              json={"objective": "min_variance"})).json()
    result = (await client.post("/api/portfolio/rebalance",
                                json={"trades": plan["trades"]})).json()

    assert result["rejected"] == 0
    assert result["filled"] == len(plan["trades"])
    assert result["portfolio"]["cash_balance"] >= 0

    # The realised book now matches what the plan predicted, within a tick of drift.
    realised = {row["ticker"]: row["weight"] for row in result["portfolio"]["positions"]}
    for row in plan["targets"]:
        if row["target_weight"] > 0.02:
            assert realised.get(row["ticker"], 0.0) == pytest.approx(
                row["target_weight"], abs=0.02
            )


@pytest.mark.asyncio
async def test_apply_reports_a_partial_batch_rather_than_failing_whole(
    client: httpx.AsyncClient,
) -> None:
    """PLAN.md §9: earlier fills stand, the failure comes back with its reason."""
    await buy(client, "MU", 10)
    result = (await client.post("/api/portfolio/rebalance", json={"trades": [
        {"ticker": "MU", "side": "sell", "quantity": 5},
        {"ticker": "MU", "side": "sell", "quantity": 999},
    ]})).json()

    assert result["filled"] == 1 and result["rejected"] == 1
    assert result["trades"][1]["code"] == "insufficient_shares"
    assert result["portfolio"]["positions"][0]["quantity"] == 5


@pytest.mark.asyncio
async def test_apply_rejects_an_empty_plan(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/portfolio/rebalance", json={"trades": []})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_apply_waits_for_an_in_flight_trade(client: httpx.AsyncClient) -> None:
    """One hold of the lock for the whole batch, so nothing interleaves mid-rebalance."""
    await buy(client, "MU", 10)
    async with portfolio.trade_lock():
        pending = asyncio.create_task(client.post("/api/portfolio/rebalance", json={
            "trades": [{"ticker": "MU", "side": "sell", "quantity": 1}],
        }))
        await asyncio.sleep(0.05)
        assert not pending.done(), "the batch ran while a trade held the lock"

    assert (await pending).json()["filled"] == 1
