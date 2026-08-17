"""The allocation maths in backend/scripts/portfolio_tool.py.

Only the pure functions — the HTTP layer is a thin shell the E2E covers end to end
(planning/REBALANCE_TEST_HARNESS.md §5).
"""

from __future__ import annotations

import random

from scripts.portfolio_tool import (
    MIN_NOTIONAL,
    equal_weights,
    random_weights,
    to_orders,
)

PRICES = {"MU": 100.0, "AMD": 50.0, "SLV": 25.0}
TICKERS = sorted(PRICES)


# ---- weights -----------------------------------------------------------------

def test_equal_weights_sum_to_one_and_are_equal() -> None:
    weights = equal_weights(TICKERS)
    assert sum(weights.values()) == 1.0
    assert set(weights.values()) == {1 / 3}


def test_weights_of_nothing_is_nothing() -> None:
    assert equal_weights([]) == {}
    assert random_weights([], random.Random(1)) == {}


def test_random_weights_sum_to_one() -> None:
    weights = random_weights(TICKERS, random.Random(42), alpha=0.6)
    assert sum(weights.values()) == 1.0
    assert all(w > 0 for w in weights.values())


def test_random_weights_are_reproducible() -> None:
    """The whole harness rests on this: a different book every run cannot be asserted on."""
    first = random_weights(TICKERS, random.Random(42), alpha=0.6)
    second = random_weights(TICKERS, random.Random(42), alpha=0.6)
    assert first == second
    assert random_weights(TICKERS, random.Random(7), alpha=0.6) != first


def test_low_concentration_is_more_lopsided() -> None:
    """--concentration has to actually concentrate, or the 'random' book is just noise
    around equal weight and the rebalancer has nothing to correct."""
    def mean_max_weight(alpha: float) -> float:
        rng = random.Random(1)
        draws = [max(random_weights(TICKERS, rng, alpha).values()) for _ in range(200)]
        return sum(draws) / len(draws)

    assert mean_max_weight(0.2) > mean_max_weight(5.0) + 0.15


# ---- orders ------------------------------------------------------------------

def test_orders_never_exceed_the_budget() -> None:
    orders, _ = to_orders(equal_weights(TICKERS), PRICES, cash=10_000.0, invest=0.95)
    assert sum(order.notional for order in orders) <= 10_000.0 * 0.95


def test_quantities_are_truncated_not_rounded() -> None:
    """$100 of a $30 stock is 3.3333… shares. Rounding up hands the last leg a bill the
    budget cannot cover; truncation cannot."""
    orders, _ = to_orders({"XYZ": 1.0}, {"XYZ": 30.0}, cash=100.0, invest=1.0)
    assert orders[0].quantity == 3.3333
    assert orders[0].notional <= 100.0


def test_orders_reconstruct_the_target_weights() -> None:
    weights = {"MU": 0.5, "AMD": 0.3, "SLV": 0.2}
    orders, _ = to_orders(weights, PRICES, cash=10_000.0, invest=1.0)
    invested = sum(order.notional for order in orders)
    realised = {order.ticker: order.notional / invested for order in orders}
    for ticker, target in weights.items():
        assert abs(realised[ticker] - target) < 1e-4


def test_unpriced_ticker_is_skipped_with_a_warning() -> None:
    orders, warnings = to_orders(
        equal_weights(["MU", "PLTR"]), {"MU": 100.0}, cash=10_000.0, invest=1.0
    )
    assert [order.ticker for order in orders] == ["MU"]
    assert any("PLTR" in warning for warning in warnings)


def test_zero_price_does_not_divide_by_zero() -> None:
    orders, warnings = to_orders({"MU": 1.0}, {"MU": 0.0}, cash=10_000.0, invest=1.0)
    assert orders == []
    assert any("MU" in warning for warning in warnings)


def test_dust_legs_are_dropped() -> None:
    """A 0.05% target on $10k is $5 — below the minimum, and not worth a blotter row."""
    orders, warnings = to_orders(
        {"MU": 0.9995, "AMD": 0.0005}, PRICES, cash=10_000.0, invest=1.0
    )
    assert [order.ticker for order in orders] == ["MU"]
    assert any(f"${MIN_NOTIONAL:.0f}" in warning for warning in warnings)
