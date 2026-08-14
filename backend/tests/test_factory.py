"""Mode detection — design §14.3. The load-bearing assertion is that startup NEVER raises:
market data always has a working fallback, so it degrades instead of failing."""

from __future__ import annotations

import pytest

import app.market as market
from app.market import Mode, PriceCache, build_market_service
from app.market.massive import MassiveLiveSource
from app.market.simulator import SimulatedSource

from .conftest import StubGateway, bar, snapshot

MARKET_ENV = ("MASSIVE_API_KEY", "SIM_SEED", "MARKET_POLL_INTERVAL_S", "MARKET_CLOSED_FALLBACK")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in MARKET_ENV:
        monkeypatch.delenv(name, raising=False)


def _install_gateway(monkeypatch: pytest.MonkeyPatch, probe_response) -> StubGateway:
    gateway = StubGateway({
        "get_snapshot_all": probe_response,
        "list_universal_snapshots": [snapshot("MU", 877.57)],
        "get_grouped_daily_aggs": lambda **kw: [bar("MU", 877.57)],
    })
    monkeypatch.setattr(market, "MassiveGateway", lambda api_key, rpm=5: gateway)
    return gateway


@pytest.mark.asyncio
async def test_no_key_is_simulated() -> None:
    service = await build_market_service(PriceCache())
    assert service.mode is Mode.SIMULATED
    assert isinstance(service._source, SimulatedSource)


@pytest.mark.parametrize("probe_response,expected", [
    ([snapshot("AAPL", 308.26)],            Mode.LIVE),
    (Exception("403 NOT_AUTHORIZED"),       Mode.ANCHORED),
    (Exception("401 UNAUTHORIZED"),         Mode.SIMULATED),
    (Exception("connection reset"),         Mode.ANCHORED),   # inconclusive -> safer
])
@pytest.mark.asyncio
async def test_mode_detection(monkeypatch: pytest.MonkeyPatch, probe_response, expected) -> None:
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    _install_gateway(monkeypatch, probe_response)
    service = await build_market_service(PriceCache())      # critically: nothing escaped
    assert service.mode is expected


@pytest.mark.asyncio
async def test_anchored_uses_the_simulator_with_massive_anchors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of ANCHORED: real levels, simulated motion, zero steady-state cost."""
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    _install_gateway(monkeypatch, Exception("403 NOT_AUTHORIZED"))
    cache = PriceCache()
    service = await build_market_service(cache)

    assert service.mode is Mode.ANCHORED
    assert isinstance(service._source, SimulatedSource)     # motion from the GBM engine
    assert service.poll_interval == 0.5                     # PLAN.md §6 cadence

    await service.start({"MU"})
    try:
        assert cache.get("MU").open_price == 877.57         # anchor from Massive
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_live_wraps_the_poller_for_closed_market_continuity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    gateway = _install_gateway(monkeypatch, [snapshot("AAPL", 308.26)])
    service = await build_market_service(PriceCache())
    assert type(service._source).__name__ == "HybridLiveSource"
    assert gateway.rpm == 300                               # bumped off the free-tier bucket


@pytest.mark.asyncio
async def test_live_without_the_fallback_is_the_bare_poller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    monkeypatch.setenv("MARKET_CLOSED_FALLBACK", "false")
    monkeypatch.setenv("MARKET_POLL_INTERVAL_S", "2")
    _install_gateway(monkeypatch, [snapshot("AAPL", 308.26)])
    service = await build_market_service(PriceCache())
    assert isinstance(service._source, MassiveLiveSource)
    assert service.poll_interval == 2.0


@pytest.mark.asyncio
async def test_a_gateway_that_cannot_be_constructed_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`massive` missing from the image, or a malformed key — still must not crash."""
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")

    def explode(api_key, rpm=5):
        raise ImportError("No module named 'massive'")

    monkeypatch.setattr(market, "MassiveGateway", explode)
    service = await build_market_service(PriceCache())
    assert service.mode is Mode.SIMULATED


@pytest.mark.parametrize("name,value", [
    ("SIM_SEED", "abc"),
    ("SIM_SEED", "4.5"),
    ("MARKET_POLL_INTERVAL_S", "abc"),
    ("MARKET_POLL_INTERVAL_S", "0"),
    ("MARKET_POLL_INTERVAL_S", "-3"),
])
@pytest.mark.asyncio
async def test_malformed_config_degrades_instead_of_crashing_startup(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str,
) -> None:
    """The factory's contract is that it never fails, and a deployment typo is exactly the
    case that contract exists for. A non-positive poll interval is the same problem one
    step later: it makes the market loop spin instead of sleeping."""
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    monkeypatch.setenv(name, value)
    _install_gateway(monkeypatch, [snapshot("AAPL", 308.26)])

    service = await build_market_service(PriceCache())
    assert service.poll_interval >= 0.1


@pytest.mark.asyncio
async def test_sim_seed_makes_a_run_reproducible(monkeypatch: pytest.MonkeyPatch) -> None:
    """D13 — required for the LLM_MOCK=true E2E runs to be reproducible."""
    monkeypatch.setenv("SIM_SEED", "42")

    async def path() -> list[tuple[str, float]]:
        # Drive the source by hand: the background loop would interleave its own polls
        # and the comparison would stop being about the seed.
        service = await build_market_service(PriceCache())
        anchors = {"AMD": 483.36, "MU": 877.57}
        await service._source.prime(sorted(anchors), anchors)
        return [
            (tick.ticker, tick.price)
            for _ in range(5)
            for tick in await service._source.poll(sorted(anchors))
        ]

    assert await path() == await path()
