"""Massive adapters against a stubbed gateway. No network, no `massive` package."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from app.market.massive import (
    MassiveAnchorProvider,
    MassiveLiveSource,
    RateLimiter,
    probe_entitlement,
)
from app.market.seeds import SEED_PRICES
from app.market.source import Entitlement

from .conftest import StubGateway, bar, snapshot

YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


def _universe_gateway(rows=None, only_on: str | None = None) -> StubGateway:
    rows = rows if rows is not None else [bar("MU", 877.57), bar("AMD", 483.36)]

    def grouped(*, date: str, adjusted: bool):
        if only_on is not None and date != only_on:
            return []
        return rows

    return StubGateway({"get_grouped_daily_aggs": grouped})


# ---- entitlement probe -------------------------------------------------

@pytest.mark.parametrize("response,expected", [
    ([snapshot("AAPL", 308.26)],              Entitlement.SNAPSHOTS),
    (Exception("403 NOT_AUTHORIZED"),         Entitlement.AGGREGATES),
    (Exception("401 UNAUTHORIZED"),           Entitlement.NONE),
    (Exception("connection reset by peer"),   Entitlement.AGGREGATES),   # safer assumption
])
@pytest.mark.asyncio
async def test_probe_entitlement(response, expected) -> None:
    gateway = StubGateway({"get_snapshot_all": response})
    assert await probe_entitlement(gateway) is expected     # and never raises


# ---- anchors -----------------------------------------------------------

@pytest.mark.asyncio
async def test_anchors_resolve_from_one_grouped_call() -> None:
    gateway = _universe_gateway()
    provider = MassiveAnchorProvider(gateway)
    resolved = await provider.anchors(["MU", "AMD"], "2026-08-12")
    assert resolved == {"MU": 877.57, "AMD": 483.36}
    assert len(gateway.calls) == 1                          # 10 tickers, 1 request


@pytest.mark.asyncio
async def test_the_universe_is_cached_per_session() -> None:
    gateway = _universe_gateway()
    provider = MassiveAnchorProvider(gateway)
    await provider.anchors(["MU"], "2026-08-12")
    await provider.anchors(["AMD"], "2026-08-12")
    assert await provider.is_known("MU", "2026-08-12") is True
    assert await provider.is_known("ZZZZ", "2026-08-12") is False
    assert len(gateway.calls) == 1                          # steady state costs 0 requests


@pytest.mark.asyncio
async def test_cold_validate_then_add_costs_one_grouped_request() -> None:
    """The watchlist-add flow validates before anchoring. Keying validation off the
    provider's own cached label instead of the caller's session loads the universe under
    "" and forces a full reload on the very next anchor lookup — two ~12,400-symbol
    requests against a 5-req/min key, for a flow documented as costing zero."""
    gateway = _universe_gateway()
    provider = MassiveAnchorProvider(gateway)

    assert await provider.is_known("MU", "2026-08-12") is True
    await provider.anchors(["MU"], "2026-08-12")

    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_refresh_reloads_the_universe() -> None:
    gateway = _universe_gateway()
    provider = MassiveAnchorProvider(gateway)
    await provider.anchors(["MU"], "2026-08-12")
    await provider.refresh("2026-08-13")
    assert len(gateway.calls) == 2


@pytest.mark.asyncio
async def test_grouped_walks_back_over_a_weekend() -> None:
    target = (date.today() - timedelta(days=3)).isoformat()
    gateway = _universe_gateway(only_on=target)
    provider = MassiveAnchorProvider(gateway)
    resolved = await provider.anchors(["MU"], "2026-08-12")
    assert resolved["MU"] == 877.57
    assert [c[1]["date"] for c in gateway.calls][0] == YESTERDAY   # starts at yesterday
    assert len(gateway.calls) == 3                                 # walked back to it


@pytest.mark.asyncio
async def test_walkback_gives_up_after_seven_days() -> None:
    gateway = _universe_gateway(only_on="never")
    provider = MassiveAnchorProvider(gateway)
    resolved = await provider.anchors(["MU"], "2026-08-12")
    grouped_calls = [c for c in gateway.calls if c[0] == "get_grouped_daily_aggs"]
    assert len(grouped_calls) == 7             # GROUPED_LOOKBACK_DAYS, then it stops
    assert resolved == {"MU": SEED_PRICES["MU"]}   # fell through to the seed table


@pytest.mark.asyncio
async def test_a_missing_symbol_falls_back_to_previous_close() -> None:
    gateway = StubGateway({
        "get_grouped_daily_aggs": lambda **kw: [bar("MU", 877.57)],
        "get_previous_close_agg": [bar("PYPL", 72.14)],       # runtime shape is a LIST
    })
    provider = MassiveAnchorProvider(gateway)
    resolved = await provider.anchors(["MU", "PYPL"], "2026-08-12")
    assert resolved == {"MU": 877.57, "PYPL": 72.14}


@pytest.mark.asyncio
async def test_an_unresolvable_symbol_is_omitted_not_invented() -> None:
    gateway = StubGateway({
        "get_grouped_daily_aggs": lambda **kw: [bar("MU", 877.57)],
        "get_previous_close_agg": Exception("404 NOT_FOUND"),
    })
    provider = MassiveAnchorProvider(gateway)
    resolved = await provider.anchors(["MU", "ZZZZ"], "2026-08-12")
    assert "ZZZZ" not in resolved                            # the service will skip it


@pytest.mark.asyncio
async def test_an_unreachable_provider_degrades_to_the_seed_table() -> None:
    gateway = StubGateway({
        "get_grouped_daily_aggs": Exception("connection refused"),
        "get_previous_close_agg": Exception("connection refused"),
    })
    provider = MassiveAnchorProvider(gateway)
    resolved = await provider.anchors(["MU", "ZZZZ"], "2026-08-12")
    assert resolved == {"MU": SEED_PRICES["MU"]}             # startup still succeeds


# ---- live source -------------------------------------------------------

@pytest.mark.asyncio
async def test_live_source_parses_nanosecond_timestamps() -> None:
    gateway = StubGateway({
        "list_universal_snapshots": [snapshot("MU", 877.57, last_updated_ns=1_786_538_412_623_000_000)]
    })
    source = MassiveLiveSource(gateway)
    tick = (await source.poll(["MU"]))[0]
    assert tick.ts == pytest.approx(1_786_538_412.623)       # ns -> float seconds


@pytest.mark.asyncio
async def test_live_source_skips_rows_carrying_an_error() -> None:
    bad = snapshot("ZZZZ", 0.0)
    bad.error = "NOT_FOUND"                # a bad ticker fails INSIDE the results array
    gateway = StubGateway({"list_universal_snapshots": [bad, snapshot("MU", 877.57)]})
    source = MassiveLiveSource(gateway)
    ticks = await source.poll(["ZZZZ", "MU"])
    assert [t.ticker for t in ticks] == ["MU"]


@pytest.mark.asyncio
async def test_live_source_requests_the_full_chunk_limit() -> None:
    gateway = StubGateway({"list_universal_snapshots": [snapshot("MU", 877.57)]})
    source = MassiveLiveSource(gateway)
    await source.poll(["MU"])
    _, kwargs = gateway.calls[0]
    assert kwargs["limit"] == 250          # the default of 10 would silently truncate


# ---- rate limiter ------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limiter_spends_its_burst_then_throttles() -> None:
    limiter = RateLimiter(rate=5, per=0.5)
    loop = asyncio.get_running_loop()
    start = loop.time()
    for _ in range(5):
        await limiter.acquire()
    assert loop.time() - start < 0.05       # the initial burst is free
    await limiter.acquire()
    assert loop.time() - start >= 0.05      # the sixth waits for the window to slide


@pytest.mark.asyncio
async def test_rate_limiter_never_exceeds_the_rate_in_a_rolling_window() -> None:
    """A token bucket passes the burst test above and still fails this one: refilling
    continuously, it releases a sixth request a fifth of the way into the window. Massive
    enforces a rolling limit, so that is a 429 during startup."""
    per = 0.4
    limiter = RateLimiter(rate=5, per=per)
    loop = asyncio.get_running_loop()

    stamps = []
    for _ in range(9):
        await limiter.acquire()
        stamps.append(loop.time())

    for i, issued in enumerate(stamps):
        window = [t for t in stamps[: i + 1] if issued - t < per]
        assert len(window) <= 5, f"{len(window)} requests inside one {per}s window"
