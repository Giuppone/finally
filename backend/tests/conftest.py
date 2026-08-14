"""Shared fixtures. No unit test touches the network — the Massive gateway is stubbed."""

from __future__ import annotations

import random
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from app import db
from app.market import MarketDataService, Mode, PriceCache
from app.market.simulator import GBMEngine, SimulatedSource, StaticAnchorProvider

FAST_POLL = 0.02          # keep the service loop tests sub-second

# Round numbers so cash and P&L assertions can be exact rather than approximate.
FIXED_PRICES = {"MU": 100.0, "AMD": 50.0, "SLV": 25.0}


@pytest.fixture
def cache() -> PriceCache:
    return PriceCache(history_maxlen=10)


@pytest_asyncio.fixture
async def temp_db(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """An initialised database in a throwaway file. Never touches the repo's db/."""
    monkeypatch.setenv("FINALLY_DB_PATH", str(tmp_path / "finally.db"))
    await db.initialize()
    return tmp_path / "finally.db"


@pytest.fixture
def priced_service() -> MarketDataService:
    """A service holding fixed prices and no running loop, so trade assertions are exact."""
    cache = PriceCache()
    service = MarketDataService(
        source=SimulatedSource(GBMEngine(seed=1), poll_interval=FAST_POLL),
        anchors=StaticAnchorProvider(random.Random(1)),
        cache=cache,
        mode=Mode.SIMULATED,
    )
    for ticker, price in FIXED_PRICES.items():
        cache.seed(ticker, price)
        service._tracked.add(ticker)
    return service


@pytest.fixture
def sim_source() -> SimulatedSource:
    return SimulatedSource(GBMEngine(seed=42), poll_interval=FAST_POLL)


@pytest.fixture
def static_anchors() -> StaticAnchorProvider:
    return StaticAnchorProvider(random.Random(42))


class FlakySource(SimulatedSource):
    """Raises on every poll — for the resilience test."""

    async def poll(self, tickers: list[str]):
        raise RuntimeError("provider down")


class StubClient:
    """Stands in for `massive.RESTClient`. Methods carry the names StubGateway keys on."""

    def get_snapshot_all(self, **kwargs: Any) -> Any: ...
    def list_universal_snapshots(self, **kwargs: Any) -> Any: ...
    def get_grouped_daily_aggs(self, **kwargs: Any) -> Any: ...
    def get_previous_close_agg(self, **kwargs: Any) -> Any: ...
    def get_market_status(self, **kwargs: Any) -> Any: ...


class StubGateway:
    """Records calls and replays canned responses. No network, no `massive` package.

    A response value that is an Exception is raised; a callable is invoked with the call
    kwargs so a test can vary the answer per request (e.g. the grouped-daily walkback).
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.client = StubClient()
        self.rpm: int | None = None

    def set_rpm(self, rpm: int) -> None:
        self.rpm = rpm

    async def call(self, fn, /, **kwargs: Any) -> Any:
        return self._replay(fn, kwargs)

    async def call_list(self, fn, /, **kwargs: Any) -> list[Any]:
        return list(self._replay(fn, kwargs) or [])

    def _replay(self, fn, kwargs: dict[str, Any]) -> Any:
        name = getattr(fn, "__name__", str(fn))
        self.calls.append((name, kwargs))
        if name not in self.responses:
            raise KeyError(f"StubGateway has no canned response for {name!r}")
        result = self.responses[name]
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result(**kwargs)
        return result


def snapshot(ticker: str, price: float, last_updated_ns: int | None = None) -> SimpleNamespace:
    """A `list_universal_snapshots` row as MassiveLiveSource reads it."""
    return SimpleNamespace(
        ticker=ticker,
        session=SimpleNamespace(price=price, close=price, last_updated=last_updated_ns),
    )


def bar(ticker: str, close: float) -> SimpleNamespace:
    """A `get_grouped_daily_aggs` row."""
    return SimpleNamespace(ticker=ticker, close=close)
