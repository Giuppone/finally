"""The HTTP surface: the SSE wire contract and the history endpoints charts seed from."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.market import MarketDataService, Mode, PriceCache, Tick, router
from app.market.routes import _price_events
from app.market.simulator import GBMEngine, SimulatedSource, StaticAnchorProvider


class StubRequest:
    """The two attributes `_price_events` actually touches."""

    def __init__(self, disconnect_after: int) -> None:
        self.client = None
        self._checks = 0
        self._limit = disconnect_after

    async def is_disconnected(self) -> bool:
        self._checks += 1
        return self._checks > self._limit


def _service(cache: PriceCache) -> MarketDataService:
    return MarketDataService(
        source=SimulatedSource(GBMEngine(seed=7)),
        anchors=StaticAnchorProvider(),
        cache=cache,
        mode=Mode.SIMULATED,
    )


async def _collect(generator, limit: int) -> list[str]:
    """Pull `limit` chunks. Leaves the generator open so a test can resume it."""
    frames = []
    async for chunk in generator:
        frames.append(chunk)
        if len(frames) >= limit:
            break
    return frames


def _payloads(frames: list[str]) -> list[dict]:
    return [
        json.loads(f.removeprefix("data: ").strip())
        for f in frames
        if f.startswith("data: ")
    ]


# ---- SSE ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_opens_with_retry_then_hello() -> None:
    cache = PriceCache()
    service = _service(cache)
    cache.seed("MU", 877.57, session_date=service.session_date)

    generator = _price_events(StubRequest(0), service)
    frames = await _collect(generator, limit=2)
    await generator.aclose()

    assert frames[0] == "retry: 1000\n\n"                   # D3
    hello = _payloads(frames)[0]
    assert hello["type"] == "hello"                         # D2: no `event:` line
    assert hello["mode"] == "simulated"
    assert hello["tick_ms"] == 500
    assert hello["poll_interval_s"] == 0.5
    assert hello["session_date"] == service.session_date
    assert hello["healthy"] is True
    assert [q["ticker"] for q in hello["quotes"]] == ["MU"]  # paintable from frame one


@pytest.mark.asyncio
async def test_stream_emits_one_frame_per_cache_change() -> None:
    """D1 + D4: one event carries every tracked ticker, and only when something moved."""
    cache = PriceCache()
    service = _service(cache)
    cache.seed("MU", 877.57)
    cache.seed("AMD", 483.36)

    generator = _price_events(StubRequest(disconnect_after=20), service)
    frames = await _collect(generator, limit=2)             # retry + hello

    async def drive() -> None:
        for i in range(3):
            await asyncio.sleep(0.6)
            cache.apply(Tick("MU", 878.0 + i, ts=float(i)))

    task = asyncio.create_task(drive())
    frames += await _collect(generator, limit=3)
    await task
    await generator.aclose()

    payloads = _payloads(frames)[1:]
    assert all(p["type"] == "prices" for p in payloads)
    assert all({q["ticker"] for q in p["quotes"]} == {"MU", "AMD"} for p in payloads)
    seqs = [p["seq"] for p in payloads]
    assert seqs == sorted(set(seqs))                        # strictly increasing, no repeats


@pytest.mark.asyncio
async def test_stream_is_silent_while_the_cache_is_unchanged() -> None:
    cache = PriceCache()
    service = _service(cache)
    cache.seed("MU", 877.57)

    generator = _price_events(StubRequest(disconnect_after=2), service)
    frames = []
    async for chunk in generator:
        frames.append(chunk)
    # The request disconnects before KEEPALIVE_S elapses, so nothing follows the hello.
    assert len(frames) == 2


@pytest.mark.asyncio
async def test_stream_ends_when_the_client_disconnects() -> None:
    cache = PriceCache()
    service = _service(cache)
    cache.seed("MU", 877.57)
    frames = [chunk async for chunk in _price_events(StubRequest(disconnect_after=1), service)]
    assert frames[0].startswith("retry:")                   # terminated, did not hang


# ---- history -----------------------------------------------------------

@pytest_asyncio.fixture
async def client():
    cache = PriceCache()
    service = _service(cache)
    for ticker, anchor in (("MU", 877.57), ("AMD", 483.36)):
        cache.seed(ticker, anchor, ts=0.0)
        for i in range(1, 200):
            cache.apply(Tick(ticker, anchor + i * 0.01, ts=float(i)))

    app = FastAPI()
    app.include_router(router)
    app.state.market = service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.mark.asyncio
async def test_bulk_history_seeds_sparklines_in_one_call(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/prices/history", params={"tickers": "mu, amd", "limit": 60})
    assert response.status_code == 200
    series = response.json()["series"]
    assert set(series) == {"MU", "AMD"}                     # normalised at the boundary
    assert len(series["MU"]) == 60
    assert series["MU"][0]["ts"] < series["MU"][-1]["ts"]
    # Subsampled across the whole buffer, not the last 60 points (D6).
    assert series["MU"][0]["ts"] < 20_000


@pytest.mark.asyncio
async def test_bulk_history_tolerates_an_untracked_symbol(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/prices/history", params={"tickers": "MU,ZZZZ"})
    assert response.status_code == 200                      # one stale symbol, whole batch
    assert response.json()["series"]["ZZZZ"] == []


@pytest.mark.asyncio
async def test_bulk_history_rejects_a_malformed_symbol(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/prices/history", params={"tickers": "MU,not a ticker"})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_ticker_history_seeds_the_main_chart(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/prices/MU/history")
    assert response.status_code == 200
    body = response.json()
    assert body["ticker"] == "MU"
    assert len(body["points"]) == 200
    assert set(body["points"][0]) == {"ts", "price"}


@pytest.mark.asyncio
async def test_ticker_history_404s_when_untracked(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/prices/ZZZZ/history")).status_code == 404


@pytest.mark.asyncio
async def test_ticker_history_400s_on_a_malformed_symbol(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/prices/not-a-ticker/history")).status_code == 400


@pytest.mark.asyncio
async def test_history_limit_is_bounded(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/prices/MU/history?limit=99999")).status_code == 422
    assert (await client.get("/api/prices/MU/history?limit=0")).status_code == 422
