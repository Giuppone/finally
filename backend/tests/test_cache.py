"""Cache invariants — design §14.2. The open_price rule is what makes daily change % mean
anything, so it is the first thing that must not regress."""

from __future__ import annotations

import pytest

from app.market import PriceCache
from app.market.models import Tick


def test_open_price_survives_a_thousand_ticks(cache: PriceCache) -> None:
    cache.seed("MU", 877.57, session_date="2026-08-12")
    for i in range(1000):
        cache.apply(Tick("MU", 800.0 + i * 0.1, ts=i))
    quote = cache.get("MU")
    assert quote is not None
    assert quote.open_price == 877.57           # the PLAN.md §13 item 3 invariant
    assert quote.session_date == "2026-08-12"   # ticks never move the session either
    assert len(quote.history) == 10             # maxlen honoured


def test_day_change_pct_is_exact(cache: PriceCache) -> None:
    cache.seed("MU", 100.0)
    cache.apply(Tick("MU", 103.5, ts=1.0))
    quote = cache.get("MU")
    assert quote is not None
    assert quote.day_change_pct == pytest.approx(3.5)
    assert quote.day_change == pytest.approx(3.5)


def test_change_and_direction_track_the_previous_tick_not_the_anchor(cache: PriceCache) -> None:
    cache.seed("MU", 100.0)
    cache.apply(Tick("MU", 110.0, ts=1.0))
    cache.apply(Tick("MU", 109.0, ts=2.0))
    quote = cache.get("MU")
    assert quote is not None
    assert quote.change == pytest.approx(-1.0)      # per-tick delta
    assert quote.direction == "down"
    assert quote.day_change_pct == pytest.approx(9.0)   # still up on the session


def test_direction_is_flat_on_an_unchanged_price(cache: PriceCache) -> None:
    cache.seed("MU", 100.0)
    cache.apply(Tick("MU", 100.0, ts=1.0))
    quote = cache.get("MU")
    assert quote is not None and quote.direction == "flat"


def test_day_change_pct_guards_a_zero_anchor(cache: PriceCache) -> None:
    cache.seed("MU", 0.0)
    cache.apply(Tick("MU", 12.0, ts=1.0))
    quote = cache.get("MU")
    assert quote is not None and quote.day_change_pct == 0.0    # no ZeroDivisionError


def test_history_subsamples_across_the_whole_buffer() -> None:
    cache = PriceCache(history_maxlen=1000)
    cache.seed("MU", 100.0, ts=0.0)
    for i in range(1, 1000):
        cache.apply(Tick("MU", 100.0 + i, ts=float(i)))
    points = cache.history("MU", limit=60)
    assert len(points) == 60
    assert points[0][0] < 50                    # starts near the beginning, not the end
    assert points[-1] == (999.0, 1099.0)        # always ends on the live price


def test_history_returns_everything_when_limit_exceeds_the_buffer(cache: PriceCache) -> None:
    cache.seed("MU", 100.0, ts=0.0)
    cache.apply(Tick("MU", 101.0, ts=1.0))
    assert len(cache.history("MU", limit=500)) == 2
    assert cache.history("NOPE", limit=10) == []


def test_version_advances_on_every_mutation(cache: PriceCache) -> None:
    versions = []
    cache.seed("MU", 100.0)
    versions.append(cache.version)
    cache.apply(Tick("MU", 101.0, ts=1.0))
    versions.append(cache.version)
    cache.evict("MU")
    versions.append(cache.version)
    assert versions == sorted(set(versions))    # strictly increasing


def test_version_does_not_advance_on_a_no_op(cache: PriceCache) -> None:
    cache.seed("MU", 100.0)
    before = cache.version
    cache.seed("MU", 999.0)                     # idempotent — never re-anchors
    cache.evict("NOPE")                         # not present
    assert cache.version == before
    quote = cache.get("MU")
    assert quote is not None and quote.open_price == 100.0


def test_apply_on_unseeded_ticker_self_seeds(cache: PriceCache) -> None:
    quote = cache.apply(Tick("NVDA", 1200.0, ts=1.0))
    assert quote.open_price == 1200.0
    assert cache.get("NVDA") is quote


def test_reanchor_moves_the_anchor_without_touching_the_price(cache: PriceCache) -> None:
    cache.seed("MU", 100.0, session_date="2026-08-12")
    cache.apply(Tick("MU", 110.0, ts=1.0))
    cache.reanchor("MU", 108.0, "2026-08-13")
    quote = cache.get("MU")
    assert quote is not None
    assert (quote.open_price, quote.session_date) == (108.0, "2026-08-13")
    assert quote.price == 110.0                 # LIVE: the API keeps supplying the price


def test_reanchor_with_rebase_gaps_the_price(cache: PriceCache) -> None:
    cache.seed("MU", 100.0, session_date="2026-08-12")
    cache.apply(Tick("MU", 110.0, ts=1.0))
    cache.reanchor("MU", 108.0, "2026-08-13", rebase=True)
    quote = cache.get("MU")
    assert quote is not None
    assert quote.price == 108.0                 # ANCHORED: the path gaps to the new close
    assert quote.prev_price == 110.0
    assert quote.day_change_pct == 0.0          # a fresh session starts at exactly 0.00%


def test_reanchor_on_an_unknown_ticker_is_a_no_op(cache: PriceCache) -> None:
    assert cache.reanchor("NOPE", 1.0, "2026-08-13") is None


def test_evict_drops_the_quote_and_its_buffer(cache: PriceCache) -> None:
    cache.seed("MU", 100.0)
    cache.evict("MU")
    assert cache.get("MU") is None
    assert cache.price("MU") is None
    assert cache.history("MU") == []
    assert cache.tickers() == set()


def test_to_wire_shape(cache: PriceCache) -> None:
    cache.seed("MU", 877.57, ts=1786538400.123, session_date="2026-08-12")
    cache.apply(Tick("MU", 877.7312, ts=1786538412.623))
    wire = cache.get("MU").to_wire()
    assert wire == {
        "ticker": "MU",
        "price": 877.7312,
        "prev_price": 877.57,
        "open_price": 877.57,
        "change": 0.1612,
        "change_pct": 0.018,
        "direction": "up",
        "ts": 1786538412623,                    # epoch MILLISECONDS on the wire (D14)
    }
