"""Conformance — design §14.1. One suite, both sources, no special-casing. This is the
test that makes "the simulator and the API look identical downstream" a fact, not a claim."""

from __future__ import annotations

import math

import pytest

from app.market.massive import MassiveLiveSource
from app.market.seeds import SEED_PRICES
from app.market.simulator import GBMEngine, SimulatedSource, StaticAnchorProvider

from .conftest import StubGateway, snapshot


def _sources():
    return [
        pytest.param(
            lambda: SimulatedSource(GBMEngine(seed=1)),
            id="simulated",
        ),
        pytest.param(
            lambda: MassiveLiveSource(
                StubGateway({"list_universal_snapshots": [snapshot("MU", 877.57)]})
            ),
            id="massive-live",
        ),
    ]


@pytest.mark.parametrize("make_source", _sources())
@pytest.mark.asyncio
async def test_source_conformance(make_source) -> None:
    source = make_source()
    await source.prime(["MU"], {"MU": 877.57})

    ticks = await source.poll(["MU"])
    assert [t.ticker for t in ticks] == ["MU"]
    assert ticks[0].price > 0 and math.isfinite(ticks[0].price)
    assert ticks[0].ts > 0

    assert await source.poll([]) == []          # empty request -> empty result
    await source.release("MU")
    await source.release("MU")                  # idempotent
    await source.aclose()


@pytest.mark.parametrize("make_source", _sources())
@pytest.mark.asyncio
async def test_poll_never_returns_untracked_tickers(make_source) -> None:
    source = make_source()
    await source.prime(["MU"], {"MU": 877.57})
    ticks = await source.poll(["MU"])
    assert {t.ticker for t in ticks} <= {"MU"}


@pytest.mark.asyncio
async def test_simulated_source_honours_the_anchor() -> None:
    source = SimulatedSource(GBMEngine(seed=1))
    await source.prime(["MU"], {"MU": 100.0})
    tick = (await source.poll(["MU"]))[0]
    assert tick.price == pytest.approx(100.0, rel=0.01)   # starts at the anchor, not $877


@pytest.mark.asyncio
async def test_simulated_source_filters_to_the_requested_set() -> None:
    source = SimulatedSource(GBMEngine(seed=1))
    await source.prime(["MU", "AMD"], {"MU": 877.57, "AMD": 483.36})
    ticks = await source.poll(["MU"])
    assert [t.ticker for t in ticks] == ["MU"]           # AMD advanced but is not reported


@pytest.mark.asyncio
async def test_simulated_source_rebase_moves_the_path() -> None:
    source = SimulatedSource(GBMEngine(seed=1))
    await source.prime(["MU"], {"MU": 877.57})
    await source.rebase("MU", 500.0)
    tick = (await source.poll(["MU"]))[0]
    assert tick.price == pytest.approx(500.0, rel=0.01)


@pytest.mark.asyncio
async def test_static_anchor_provider() -> None:
    provider = StaticAnchorProvider()
    resolved = await provider.anchors(["MU", "ZZZZ"], "2026-08-12")
    assert resolved["MU"] == SEED_PRICES["MU"]            # the seed table
    assert 40.0 <= resolved["ZZZZ"] <= 400.0              # plausible fallback level
    assert await provider.is_known("ANYTHING") is True    # regex is the only gate
    await provider.refresh("2026-08-13")
