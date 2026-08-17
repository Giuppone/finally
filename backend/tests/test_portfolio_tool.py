"""The allocation maths in backend/scripts/portfolio_tool.py.

Only the pure functions — the HTTP layer is a thin shell the E2E covers end to end
(planning/REBALANCE_TEST_HARNESS.md §5).
"""

from __future__ import annotations

import random

import pytest

from scripts import portfolio_tool
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


# ---- --dry-run must not write ------------------------------------------------

class RecordingApi:
    """Stands in for `Api`, recording every call so a test can assert on the verbs."""

    PRICES = {"MU": 100.0, "AMD": 50.0, "SLV": 25.0}

    def __init__(self, base: str, timeout: float = 20.0) -> None:
        self.base = base
        self.calls: list[tuple[str, str]] = []

    def get(self, path: str) -> dict:
        self.calls.append(("GET", path))
        if path == "/api/health":
            return {"status": "ok"}
        if path == "/api/watchlist":
            return {"tickers": [{"ticker": t, "priced": True, "price": p}
                                for t, p in self.PRICES.items()]}
        if path == "/api/portfolio":
            return {"cash_balance": 10_000.0, "positions": [], "positions_value": 0.0,
                    "total_value": 10_000.0}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, payload: dict | None = None) -> dict:
        self.calls.append(("POST", path))
        return {}


def run_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> RecordingApi:
    recorded: list[RecordingApi] = []

    def factory(base: str, timeout: float = 20.0) -> RecordingApi:
        api = RecordingApi(base, timeout)
        recorded.append(api)
        return api

    monkeypatch.setattr(portfolio_tool, "Api", factory)
    args = portfolio_tool.build_parser().parse_args(argv)
    assert args.func(args) == 0
    return recorded[0]


@pytest.mark.parametrize("mode", ["equal", "random"])
def test_dry_run_sends_no_writes(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """`--dry-run` says "print the plan, send no writes" and has to mean it.

    It did not: the reset ran before the plan was ever printed, so the flag a user reaches
    for to try the script safely was the one that emptied their account.
    """
    api = run_cli(monkeypatch, [mode, "--dry-run", "--yes"])
    assert [call for call in api.calls if call[0] == "POST"] == []
    assert ("GET", "/api/health") in api.calls


def test_dry_run_does_not_add_tickers_to_the_watchlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other write hiding in the same path: an unwatched --tickers entry was POSTed
    to the watchlist before the plan was printed."""
    api = run_cli(monkeypatch, ["equal", "--dry-run", "--yes", "--tickers", "MU,NVDA"])
    assert [call for call in api.calls if call[0] == "POST"] == []


def test_a_real_run_does_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must not have turned the reset off for everyone."""
    api = run_cli(monkeypatch, ["equal", "--yes"])
    assert ("POST", "/api/portfolio/reset") in api.calls


def test_no_reset_skips_the_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    api = run_cli(monkeypatch, ["equal", "--yes", "--no-reset"])
    assert ("POST", "/api/portfolio/reset") not in api.calls
