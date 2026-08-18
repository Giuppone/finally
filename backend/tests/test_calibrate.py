"""The calibration maths and the bars cache (MARKET_SIMULATOR.md section 9).

No network: every test feeds closes in directly. The one function that talks to the API,
`fetch_bars`, is a thin urllib shell around a documented endpoint.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pytest

from app.market.seeds import TICKER_PARAMS
from scripts import calibrate_market as cal


def geometric(start: float, daily_log_return: float, n: int) -> list[float]:
    """A perfectly smooth series: zero volatility, known drift."""
    return [start * math.exp(daily_log_return * i) for i in range(n)]


# ---- the formulas ------------------------------------------------------------

def test_log_returns_of_a_flat_series_are_zero() -> None:
    assert cal.log_returns([100.0] * 5) == [0.0, 0.0, 0.0, 0.0]


def test_sigma_of_a_smooth_series_is_zero() -> None:
    """No dispersion, no volatility - however fast it climbs."""
    assert cal.annualised_sigma(cal.log_returns(geometric(100, 0.01, 60))) == pytest.approx(0.0)


def test_sigma_annualises_by_root_252() -> None:
    """A daily standard deviation of s becomes s*sqrt(252) a year.

    1% tolerance because `statistics.stdev` is the SAMPLE deviation: over 80 points the
    Bessel correction inflates it by sqrt(80/79), about 0.6%.
    """
    returns = [0.01, -0.01] * 40
    daily = 0.01                       # population stdev of a symmetric two-point series
    assert cal.annualised_sigma(returns) == pytest.approx(daily * math.sqrt(252), rel=1e-2)


def test_drift_is_the_annualised_mean_LOG_return() -> None:
    """0.002 per day over 252 days is 0.504 - a log-drift, not a 50.4% simple return."""
    assert cal.annualised_drift(cal.log_returns(geometric(100, 0.002, 100))) == pytest.approx(
        0.002 * 252, rel=1e-6
    )


def test_cagr_and_drift_are_different_quantities() -> None:
    """CAGR = exp(drift) - 1. Confusing them is a units error that the 0.20 damping cap
    happens to hide on the fastest names and does not hide on the slower ones.

    The identity only holds when the bar count matches the annualisation factor, so this
    uses 252 returns over 365 calendar days - one trading year. Build it from 365 daily
    bars instead and the two disagree for a second, unrelated reason: the series would
    contain a year of CALENDAR moves annualised as though they were TRADING days.
    """
    closes = geometric(100, 0.002, 253)          # 252 returns = one trading year
    drift = cal.annualised_drift(cal.log_returns(closes))
    growth = cal.cagr(closes[0], closes[-1], 365)

    assert drift == pytest.approx(0.002 * 252, abs=1e-6)
    assert growth == pytest.approx(math.exp(drift) - 1, rel=1e-2)
    assert growth == pytest.approx(0.65, abs=1e-2)       # 65%, not the 50.4% log-drift


def test_cagr_of_a_flat_series_is_zero() -> None:
    assert cal.cagr(100.0, 100.0, 365) == pytest.approx(0.0)


def test_cagr_guards_degenerate_inputs() -> None:
    assert cal.cagr(0.0, 100.0, 365) == 0.0
    assert cal.cagr(100.0, 0.0, 365) == 0.0
    assert cal.cagr(100.0, 110.0, 0) == 0.0


# ---- the damping rule --------------------------------------------------------

@pytest.mark.parametrize("realised, expected", [
    (2.299, 0.20),      # MU     - capped
    (1.600, 0.16),      # ALAB
    (1.722, 0.17),      # INTC
    (0.237, 0.02),      # PLTR
    (0.414, 0.04),      # SLV
    (1.643, 0.16),      # MRVL
    (1.421, 0.14),      # AMD
    (0.735, 0.07),      # ANET
    (1.270, 0.13),      # LRCX
    (1.312, 0.13),      # AMAT
])
def test_damping_reproduces_every_shipped_value(realised: float, expected: float) -> None:
    """The rule is 10% of realised, capped at 0.20 (MARKET_SIMULATOR.md section 6), and these
    are the realised figures from its section 2 table. If a future edit changes the rule,
    this catches it before a regeneration silently rewrites ten calibrated tickers."""
    assert cal.damp(realised) == expected


def test_damping_uses_realised_drift_not_cagr() -> None:
    """PLTR is the case that exposes the difference: damping its log-drift gives the shipped
    0.02, damping its CAGR would give 0.03. On MU the cap masks the same mistake."""
    realised = 0.237
    assert cal.damp(realised) == 0.02
    assert cal.damp(math.exp(realised) - 1) == 0.03      # what the wrong input produces


def test_damping_caps_at_the_ceiling() -> None:
    assert cal.damp(50.0) == 0.20
    assert cal.damp(2.0) == 0.20


def test_shipped_params_are_reachable_by_the_rule() -> None:
    """Every mu currently in TICKER_PARAMS must be a value the damping rule can produce -
    at or below the cap, and on the 2-decimal grid."""
    for ticker, params in TICKER_PARAMS.items():
        assert params["mu"] <= 0.20, ticker
        assert params["mu"] == round(params["mu"], 2), ticker
        assert params["sigma"] > 0, ticker


# ---- correlation and alignment ----------------------------------------------

def test_a_series_correlates_perfectly_with_itself() -> None:
    returns = [0.01, -0.02, 0.005, 0.03, -0.01]
    assert cal.pearson(returns, returns) == pytest.approx(1.0)


def test_an_inverted_series_correlates_minus_one() -> None:
    returns = [0.01, -0.02, 0.005, 0.03, -0.01]
    assert cal.pearson(returns, [-r for r in returns]) == pytest.approx(-1.0)


def test_pearson_of_a_constant_series_is_zero_not_a_crash() -> None:
    """Zero variance means an undefined correlation; returning 0 keeps the matrix usable."""
    assert cal.pearson([0.01] * 5, [0.01, 0.02, 0.03, 0.04, 0.05]) == 0.0


def test_alignment_keeps_only_days_every_ticker_has() -> None:
    """A ticker that listed late must not silently shorten the others' history to a window
    they were never measured over - and must not extend its own with gaps either."""
    days, aligned = cal.align({
        "A": {1: 10.0, 2: 11.0, 3: 12.0, 4: 13.0},
        "B": {2: 20.0, 3: 21.0, 4: 22.0},
        "C": {1: 30.0, 2: 31.0, 3: 32.0, 4: 33.0},
    })
    assert days == [2, 3, 4]
    assert aligned["A"] == [11.0, 12.0, 13.0]
    assert aligned["B"] == [20.0, 21.0, 22.0]
    assert all(len(closes) == 3 for closes in aligned.values())


def test_alignment_of_nothing_is_empty() -> None:
    assert cal.align({}) == ([], {})


# ---- the cache: the whole point is not refetching ---------------------------

def fresh_entry(start: str = "2025-12-01", end: str = "2026-08-14",
                age_days: int = 0) -> dict:
    fetched = datetime.now(tz=timezone.utc) - timedelta(days=age_days)
    return {"start": start, "end": end, "fetched_at": fetched.isoformat(timespec="seconds"),
            "closes": [[20000, 100.0], [20001, 101.0]]}


def test_a_recent_entry_covering_the_window_is_reused() -> None:
    assert cal.is_fresh(fresh_entry(), "2025-12-01", "2026-08-14", 30) is True


def test_a_stale_entry_is_refetched() -> None:
    assert cal.is_fresh(fresh_entry(age_days=45), "2025-12-01", "2026-08-14", 30) is False


def test_an_entry_that_does_not_cover_the_window_is_refetched() -> None:
    """Coverage matters as much as age: a 3-month pull cannot answer an 8-month request,
    however recently it was fetched."""
    short = fresh_entry(start="2026-06-01")
    assert cal.is_fresh(short, "2025-12-01", "2026-08-14", 30) is False


def test_a_missing_or_empty_entry_is_refetched() -> None:
    assert cal.is_fresh(None, "2025-12-01", "2026-08-14", 30) is False
    assert cal.is_fresh({"closes": []}, "2025-12-01", "2026-08-14", 30) is False
    assert cal.is_fresh({"closes": [[1, 2.0]]}, "2025-12-01", "2026-08-14", 30) is False


def test_a_corrupt_timestamp_is_refetched_rather_than_crashing() -> None:
    entry = fresh_entry() | {"fetched_at": "not a date"}
    assert cal.is_fresh(entry, "2025-12-01", "2026-08-14", 30) is False


def test_cache_round_trips(tmp_path) -> None:
    path = tmp_path / "bars.json"
    cache = {"version": cal.CACHE_VERSION, "tickers": {"MU": fresh_entry()}}
    cal.save_cache(cache, path)
    assert cal.load_cache(path) == cache


def test_a_missing_cache_starts_empty(tmp_path) -> None:
    assert cal.load_cache(tmp_path / "absent.json") == {"version": cal.CACHE_VERSION,
                                                        "tickers": {}}


def test_a_future_cache_version_is_refused(tmp_path) -> None:
    """Better to stop than to read a layout this script does not understand."""
    path = tmp_path / "bars.json"
    path.write_text('{"version": 99, "tickers": {}}', encoding="utf-8")
    with pytest.raises(cal.CalibrationError, match="version"):
        cal.load_cache(path)


# ---- window selection --------------------------------------------------------

@pytest.mark.parametrize("today, expected", [
    (date(2026, 8, 17), date(2026, 8, 14)),     # Monday   -> Friday
    (date(2026, 8, 16), date(2026, 8, 14)),     # Sunday   -> Friday
    (date(2026, 8, 15), date(2026, 8, 14)),     # Saturday -> Friday
    (date(2026, 8, 14), date(2026, 8, 13)),     # Friday   -> Thursday
])
def test_the_window_ends_before_today(today: date, expected: date) -> None:
    """A Basic key refuses a `to` of today - "Your plan doesn't include this data
    timeframe" - and there are no bars on a weekend either."""
    assert cal.previous_trading_day(today) == expected


# ---- end to end over synthetic bars -----------------------------------------

def test_measure_produces_a_consistent_row() -> None:
    closes = geometric(100.0, 0.003, 200)
    measured = cal.measure("TEST", closes, calendar_days=280)

    assert measured.bars == 200
    assert measured.last_close == closes[-1]
    assert measured.sigma == pytest.approx(0.0, abs=1e-9)      # smooth series
    assert measured.mu_realised == pytest.approx(0.003 * 252, rel=1e-6)
    assert measured.mu == cal.damp(measured.mu_realised)
    # CAGR is the simple return over the window, and exceeds the log-drift.
    assert measured.cagr > measured.mu_realised
