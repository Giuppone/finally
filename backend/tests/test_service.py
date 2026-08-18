"""The service: tracked set, immediate add, session roll, resilience — design §14.4/§14.6."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.market import MarketDataService, Mode, PriceCache, Tick
from app.market.seeds import SEED_PRICES
from app.market.service import current_session_date
from app.market.simulator import GBMEngine, SimulatedSource, StaticAnchorProvider

from .conftest import FAST_POLL, FlakySource


class MovableAnchors(StaticAnchorProvider):
    """Anchors a test can move between sessions, to drive the rollover paths."""

    def __init__(self, price: float) -> None:
        super().__init__()
        self.price = price

    async def anchors(self, tickers, session_date=""):
        return {t: self.price for t in tickers}


def _service(source=None, anchors=None, cache=None, mode=Mode.SIMULATED) -> MarketDataService:
    return MarketDataService(
        source=source or SimulatedSource(GBMEngine(seed=42), poll_interval=FAST_POLL),
        anchors=anchors or StaticAnchorProvider(),
        cache=cache or PriceCache(),
        mode=mode,
    )


@pytest.mark.asyncio
async def test_start_seeds_every_tracked_ticker(static_anchors) -> None:
    cache = PriceCache()
    service = _service(anchors=static_anchors, cache=cache)
    await service.start({"MU", "AMD"})
    try:
        assert service.tracked == {"MU", "AMD"}
        assert cache.get("MU").open_price == SEED_PRICES["MU"]   # priced from the first frame
        assert cache.get("MU").session_date == service.session_date
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_position_ticker_keeps_ticking_after_watchlist_removal(static_anchors) -> None:
    """PLAN.md §13 item 4. The bug this catches is silent: the position keeps a frozen
    price and portfolio value, the heatmap and the P&L chart all quietly go stale."""
    cache = PriceCache()
    service = _service(anchors=static_anchors, cache=cache)
    await service.start({"MU", "AMD"})
    try:
        # User removes MU from the watchlist but still holds the position.
        await service.sync_tracked({"AMD", "MU"})          # union still contains MU
        before = cache.get("MU").price
        await asyncio.sleep(FAST_POLL * 10)
        assert cache.get("MU") is not None
        assert cache.get("MU").price != before             # still updating

        # Now the position is closed too -> MU leaves the union and is evicted.
        await service.sync_tracked({"AMD"})
        assert cache.get("MU") is None
        assert service.tracked == {"AMD"}
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_sync_tracked_adds_and_prices_new_tickers(static_anchors) -> None:
    cache = PriceCache()
    service = _service(anchors=static_anchors, cache=cache)
    await service.start({"MU"})
    try:
        await service.sync_tracked({"MU", "SLV"})
        assert service.tracked == {"MU", "SLV"}
        assert cache.get("SLV").open_price == SEED_PRICES["SLV"]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_add_ticker_returns_a_priced_quote(static_anchors) -> None:
    """D10: the route must be able to respond with a price already in hand, and the LLM's
    auto-add path must have something to fill a trade against."""
    service = _service(anchors=static_anchors)
    quote = await service.add_ticker("PLTR")
    assert quote is not None
    assert quote.price > 0
    assert quote.open_price == SEED_PRICES["PLTR"]
    assert "PLTR" in service.tracked


@pytest.mark.asyncio
async def test_add_ticker_is_idempotent(static_anchors) -> None:
    service = _service(anchors=static_anchors)
    first = await service.add_ticker("PLTR")
    second = await service.add_ticker("PLTR")
    assert first is second
    assert service.tracked == {"PLTR"}


@pytest.mark.asyncio
async def test_add_ticker_returns_none_when_it_cannot_be_anchored() -> None:
    class NoAnchors(StaticAnchorProvider):
        async def anchors(self, tickers, session_date=""):
            return {}

    service = _service(anchors=NoAnchors())
    assert await service.add_ticker("ZZZZ") is None
    assert service.tracked == set()                        # never tracked priceless


@pytest.mark.asyncio
async def test_concurrent_adds_prime_a_ticker_once(static_anchors) -> None:
    service = _service(anchors=static_anchors)
    results = await asyncio.gather(*[service.add_ticker("MU") for _ in range(5)])
    assert service.tracked == {"MU"}
    assert all(q is not None for q in results)


@pytest.mark.asyncio
async def test_price_and_quote_are_none_before_the_first_tick(static_anchors) -> None:
    """D15: never invent a price. Callers fall back to avg_cost rather than valuing a
    position at 0, which would render -100% P&L and poison the snapshot table."""
    service = _service(anchors=static_anchors)
    assert service.price("MU") is None
    assert service.quote("MU") is None


@pytest.mark.asyncio
async def test_failed_polls_preserve_the_last_price_and_flip_healthy(static_anchors) -> None:
    cache = PriceCache()
    service = _service(
        source=FlakySource(GBMEngine(seed=1), poll_interval=FAST_POLL),
        anchors=static_anchors,
        cache=cache,
    )
    await service.start({"MU"})
    try:
        seeded = cache.get("MU").price
        await asyncio.sleep(FAST_POLL * 40)
        assert cache.get("MU").price == seeded             # stale beats blank
        assert service.healthy is False
        assert service._backoff() <= 60.0                  # bounded
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_backoff_grows_then_stops_doubling() -> None:
    service = _service()
    assert service._backoff() == service.poll_interval     # healthy: no penalty
    service._failures = 1
    first = service._backoff()
    service._failures = 3
    assert service._backoff() > first
    service._failures = 99
    # The doubling stops at 2**5, so a 0.5s simulator cadence never reaches MAX_BACKOFF.
    assert service._backoff() == service.poll_interval * 32


@pytest.mark.asyncio
async def test_backoff_caps_at_the_massive_poll_cadence() -> None:
    """Where MAX_BACKOFF actually bites: a 15s LIVE poll would otherwise back off to 8
    minutes, and a burst of 429s on a free key would hammer the rate limit forever."""
    service = _service(source=SimulatedSource(GBMEngine(seed=1), poll_interval=15.0))
    service._failures = 99
    assert service._backoff() == 60.0


@pytest.mark.asyncio
async def test_empty_tracked_set_idles_without_error(static_anchors) -> None:
    service = _service(anchors=static_anchors)
    await service.start(set())
    try:
        await asyncio.sleep(FAST_POLL * 5)
        assert service.tracked == set()
        assert service.healthy is True                     # idling is not a failure
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_health_fragment_shape(static_anchors) -> None:
    service = _service(anchors=static_anchors)
    await service.start({"MU"})
    try:
        await asyncio.sleep(FAST_POLL * 3)
        health = service.health()
        assert health["mode"] == "simulated"
        assert health["source"] == "SimulatedSource"
        assert health["healthy"] is True
        assert health["tracked"] == 1
        assert health["session_date"] == service.session_date
        assert health["last_tick_age_s"] is not None
        assert health["market_status"] is None             # non-null only under Hybrid
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_stop_is_safe_before_and_after_start(static_anchors) -> None:
    service = _service(anchors=static_anchors)
    await service.stop()                                   # never started
    await service.start({"MU"})
    await service.stop()
    await service.stop()                                   # idempotent


# ---- session rollover (design §9.3) -----------------------------------------

@pytest.mark.asyncio
async def test_simulated_roll_pins_the_anchor_to_the_current_price(static_anchors) -> None:
    cache = PriceCache()
    service = _service(anchors=static_anchors, cache=cache, mode=Mode.SIMULATED)
    await service.start({"MU"})
    await service.stop()                                   # drive the roll by hand

    cache.apply(Tick("MU", 900.0, ts=1.0))
    service._session_date = "1999-01-01"                   # force a roll
    await service._maybe_roll_session()

    quote = cache.get("MU")
    assert quote.open_price == 900.0                       # no price discontinuity
    assert quote.price == 900.0
    assert quote.day_change_pct == 0.0
    assert quote.session_date == current_session_date()


@pytest.mark.asyncio
async def test_anchored_roll_gaps_the_path_onto_the_new_close() -> None:
    cache = PriceCache()
    source = SimulatedSource(GBMEngine(seed=1), poll_interval=FAST_POLL)
    anchors = MovableAnchors(877.57)

    service = _service(source=source, anchors=anchors, cache=cache, mode=Mode.ANCHORED)
    await service.start({"MU"})
    await service.stop()

    anchors.price = 950.0                                  # yesterday's close, next session
    service._session_date = "1999-01-01"
    await service._maybe_roll_session()

    quote = cache.get("MU")
    assert quote.open_price == 950.0
    assert quote.price == 950.0                            # rebased — reads as a gap
    assert source._engine.price("MU") == 950.0             # engine follows the cache


@pytest.mark.asyncio
async def test_roll_is_a_no_op_within_the_same_session(static_anchors) -> None:
    cache = PriceCache()
    service = _service(anchors=static_anchors, cache=cache)
    await service.start({"MU"})
    await service.stop()
    version = cache.version
    await service._maybe_roll_session()                    # same session_date
    assert cache.version == version


def test_current_session_date_rolls_at_0930_et() -> None:
    # 13:00 UTC == 09:00 ET -> still the previous session.
    before = datetime(2026, 8, 13, 13, 0, tzinfo=timezone.utc)
    after = datetime(2026, 8, 13, 14, 0, tzinfo=timezone.utc)   # 10:00 ET
    assert current_session_date(before) == "2026-08-12"
    assert current_session_date(after) == "2026-08-13"


def test_session_date_does_not_invent_weekend_sessions() -> None:
    """A calendar label would roll three times across a weekend — Sat, Sun and again
    before Monday's open — each spending a grouped-daily walkback on a 5-req/min key and
    rebasing the ANCHORED path to the same Friday close it already had."""
    friday = "2026-08-14"
    for moment in (
        datetime(2026, 8, 15, 16, 0, tzinfo=timezone.utc),   # Sat 12:00 ET
        datetime(2026, 8, 16, 16, 0, tzinfo=timezone.utc),   # Sun 12:00 ET
        datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc),   # Mon 08:00 ET, pre-open
    ):
        assert current_session_date(moment) == friday

    monday = datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc)   # Mon 10:00 ET
    assert current_session_date(monday) == "2026-08-17"          # rolls exactly once


@pytest.mark.asyncio
async def test_concurrent_reconciliations_converge_on_the_last_desired_set() -> None:
    """Releasing the lock between removals and additions lets two reconciliations each
    compute their addition against the same emptied set, leaving the union of both
    requests tracked instead of the last one."""
    cache = PriceCache()
    service = _service(anchors=MovableAnchors(100.0), cache=cache)
    await service.start({"MU"})
    await service.stop()

    await asyncio.gather(
        service.sync_tracked({"AMD"}),
        service.sync_tracked({"AMD"}),
    )
    assert service.tracked == {"AMD"}
    assert cache.get("MU") is None

    results = await asyncio.gather(
        service.sync_tracked({"SLV"}),
        service.sync_tracked({"INTC"}),
        return_exceptions=True,
    )
    assert not any(isinstance(r, Exception) for r in results)
    # Whichever ran last wins outright; the loser must not leave its ticker behind.
    assert service.tracked in ({"SLV"}, {"INTC"})
    assert set(cache.tickers()) == service.tracked


@pytest.mark.asyncio
async def test_a_holiday_roll_does_not_fake_an_overnight_gap() -> None:
    """The weekday guard still admits market holidays. When the anchor comes back
    unchanged no session printed, so rebasing would yank the path back to a close it
    already gapped from — on a day the market never opened."""
    cache = PriceCache()
    source = SimulatedSource(GBMEngine(seed=1), poll_interval=FAST_POLL)
    service = _service(source=source, anchors=MovableAnchors(877.57), cache=cache,
                       mode=Mode.ANCHORED)
    await service.start({"MU"})
    await service.stop()

    cache.apply(Tick("MU", 900.0, ts=1.0))
    service._session_date = "1999-01-01"
    await service._maybe_roll_session()             # same anchor comes back

    quote = cache.get("MU")
    assert quote.open_price == 877.57
    assert quote.price == 900.0                     # path untouched, no fake gap


# ---- add_ticker isolation (Market_data_review.md P2) -------------------------

@pytest.mark.asyncio
async def test_add_ticker_does_not_move_the_other_tracked_paths() -> None:
    """The review's own reproduction, as a regression test.

    add_ticker() does one immediate poll so the new ticker comes back priced. Before the
    fix that poll stepped EVERY registered path, so MU moved here without ever reporting
    a tick — its next regular poll then showed two compounded steps as one, and SIM_SEED
    reproducibility broke for any run that adds a ticker mid-flight.
    """
    engine = GBMEngine(seed=42)
    service = MarketDataService(
        source=SimulatedSource(engine, poll_interval=1000),   # loop never fires
        anchors=StaticAnchorProvider(),
        cache=PriceCache(),
        mode=Mode.SIMULATED,
    )
    await service.start({"MU"})
    try:
        before = engine.price("MU")
        cached_before = service.quote("MU").price

        assert await service.add_ticker("AMD") is not None

        assert engine.price("MU") == before
        assert service.quote("MU").price == cached_before
    finally:
        await service.stop()
