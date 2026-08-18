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


# ---- holdings lists ----------------------------------------------------------

def test_list_accepts_every_reasonable_separator() -> None:
    """This file is meant to be typed by a human. A format that rejects the obvious
    spellings is a format people stop using."""
    holdings, _, warnings = portfolio_tool.parse_list(
        "MU 10\nAMD:  5\nSLV, 40\nPLTR = 2.5\n"
    )
    assert holdings == [("MU", 10.0), ("AMD", 5.0), ("SLV", 40.0), ("PLTR", 2.5)]
    assert warnings == []


def test_list_ignores_comments_and_blank_lines() -> None:
    holdings, _, warnings = portfolio_tool.parse_list(
        "# a header\n\n  \nMU 10   # a trailing note\n"
    )
    assert holdings == [("MU", 10.0)]
    assert warnings == []


def test_list_lowercase_tickers_are_normalised() -> None:
    holdings, _, _ = portfolio_tool.parse_list("mu 10\n")
    assert holdings == [("MU", 10.0)]


@pytest.mark.parametrize("line, fragment", [
    ("BADROW", "expected 'TICKER QTY'"),
    ("MU lots", "not a number"),
    ("MU -3", "not positive"),
    ("MU 0", "not positive"),
])
def test_a_bad_row_is_reported_and_skipped(line: str, fragment: str) -> None:
    """One fat-fingered row must not throw away the other nine."""
    holdings, _, warnings = portfolio_tool.parse_list(f"AMD 5\n{line}\nSLV 2\n")
    assert [h[0] for h in holdings] == ["AMD", "SLV"]
    assert any(fragment in warning for warning in warnings)


def test_a_repeated_ticker_is_skipped_not_merged() -> None:
    """Two rows for one ticker have no single correct reading - summing them would invent a
    position the user did not write."""
    holdings, _, warnings = portfolio_tool.parse_list("MU 10\nMU 5\n")
    assert holdings == [("MU", 10.0)]
    assert any("twice" in warning for warning in warnings)


def test_an_empty_list_yields_nothing() -> None:
    assert portfolio_tool.parse_list("# only comments\n\n") == ([], "shares", [])


def test_dump_round_trips_through_the_parser() -> None:
    """What `dump` writes, `build` must be able to read - including the header."""
    original = [("ALAB", 2.989), ("MU", 0.982), ("SLV", 16.3215)]
    holdings, mode, warnings = portfolio_tool.parse_list(
        portfolio_tool.format_list(original, ["# a header"])
    )
    assert mode == "shares"
    assert holdings == original
    assert warnings == []


# ---- weights in a list -------------------------------------------------------

def test_a_percent_row_is_a_weight() -> None:
    holdings, mode, warnings = portfolio_tool.parse_list("MU 40%\nAMD 60%\n")
    assert mode == "weight"
    assert holdings == [("MU", 0.4), ("AMD", 0.6)]
    assert warnings == []


def test_mixing_shares_and_weights_is_refused() -> None:
    """"4 shares of MU and 30% of AMD" has no combined reading, so guessing one is worse
    than saying so."""
    with pytest.raises(ValueError, match="mixes"):
        portfolio_tool.parse_list("MU 4\nAMD 30%\n")


def test_weight_list_round_trips_through_the_formatter() -> None:
    original = [("MU", 0.1008), ("ASML", 0.0954)]
    holdings, mode, _ = portfolio_tool.parse_list(
        portfolio_tool.format_list(original, ["# a header"], mode="weight")
    )
    assert mode == "weight"
    for (ticker, weight), (original_ticker, original_weight) in zip(holdings, original):
        assert ticker == original_ticker
        assert weight == pytest.approx(original_weight, abs=1e-5)


# ---- broker export -----------------------------------------------------------

BROKER_SAMPLE = """MU
CEDEAR MICRON TECHNOLOGY INC
45$305.650 0,00%$221.905,56$3.773.000,00
 37,78%
$13.754.250,00 AMZN
CEDEAR AMAZON.COM, INC
1.343$2.872,50 0,00%$2.295,96$777.645,00
 25,22%
$3.857.767,50 TGNO4
TRANS GAS DEL NORTE "C" ORD $
437$3.345,00 0,00%$4.460,00-$485.070,00
 -24,89%
$1.461.765,00"""


def test_argentine_numbers_are_read_the_argentine_way() -> None:
    """'.' groups thousands and ',' is the decimal point. Reading '305.650' the American
    way understates the holding by a factor of a thousand."""
    assert portfolio_tool.parse_amount("305.650") == 305_650.0
    assert portfolio_tool.parse_amount("221.905,56") == pytest.approx(221_905.56)
    assert portfolio_tool.parse_amount("2.872,50") == pytest.approx(2_872.50)
    assert portfolio_tool.parse_amount("45") == 45.0


def test_broker_export_parses_every_field() -> None:
    rows, warnings = portfolio_tool.parse_broker(BROKER_SAMPLE)
    assert warnings == []
    assert [row.ticker for row in rows] == ["MU", "AMZN", "TGNO4"]

    mu = rows[0]
    assert mu.quantity == 45
    assert mu.price == pytest.approx(305_650.0)
    assert mu.market_value == pytest.approx(13_754_250.0)
    assert mu.is_cedear is True


def test_a_locally_listed_row_is_marked() -> None:
    """The 'CEDEAR' prefix is what says a row has a US underlying. TGNO4 does not, so it
    cannot be mapped to a US ticker and the converter drops it."""
    rows, _ = portfolio_tool.parse_broker(BROKER_SAMPLE)
    assert rows[2].ticker == "TGNO4"
    assert rows[2].is_cedear is False


def test_a_row_whose_arithmetic_does_not_reconcile_is_rejected() -> None:
    """quantity x price must equal the stated market value. That is a free correctness check
    on an undocumented format, and it is what would catch a decimal-separator mistake -
    which no looser check would, since the result parses perfectly well as a number."""
    corrupted = BROKER_SAMPLE.replace("$13.754.250,00", "$99.999.999,00")
    rows, warnings = portfolio_tool.parse_broker(corrupted)
    assert [row.ticker for row in rows] == ["AMZN", "TGNO4"]
    assert any("MU" in warning and "did not parse cleanly" in warning
               for warning in warnings)


def test_an_unrecognised_file_says_so_rather_than_returning_nothing() -> None:
    rows, warnings = portfolio_tool.parse_broker("this is not a broker export\n")
    assert rows == []
    assert any("right file" in warning for warning in warnings)
