"""GBM maths — design §14.5 and MARKET_SIMULATOR.md §11."""

from __future__ import annotations

import math
import statistics

import pytest

from app.market import simulator
from app.market.seeds import SEED_WATCHLIST
from app.market.simulator import (
    JUMP_MAX,
    SECONDS_PER_TRADING_YEAR,
    GBMEngine,
    _cholesky,
    correlation_matrix,
    diffusion_sigma,
    sector_rho,
)

# 400k ticks sampled every 1000 gives ~400 observations: enough that the estimator's
# standard error (~3.5%) sits well inside the ±15% tolerance below.
VOL_STEPS = 400_000
VOL_SAMPLE_EVERY = 1_000


def _log_returns(prices: list[float]) -> list[float]:
    return [math.log(b / a) for a, b in zip(prices, prices[1:])]


def measure_annualised_sigma(ticker: str, *, steps: int, sample_every: int, seed: int) -> float:
    engine = GBMEngine(seed=seed)
    engine.add_ticker(ticker)
    samples = []
    for i in range(steps):
        price = engine.step()[ticker]
        if i % sample_every == 0:
            samples.append(price)
    interval_years = sample_every * 0.5 / SECONDS_PER_TRADING_YEAR
    return statistics.stdev(_log_returns(samples)) / math.sqrt(interval_years)


def test_volatility_budget_is_exact() -> None:
    engine = GBMEngine(seed=0)
    sigma_d = diffusion_sigma(0.885, engine.jump_variance)
    assert sigma_d**2 + engine.jump_variance == pytest.approx(0.885**2)


def test_diffusion_sigma_clamps_below_the_jump_floor() -> None:
    # A low-vol ticker below sigma_jump (0.357) must clamp, not sqrt a negative.
    assert diffusion_sigma(0.10, 0.1278) > 0


def test_jump_variance_matches_the_documented_value() -> None:
    # MARKET_SIMULATOR.md §5: 0.1278, ~16% of total variance at sigma = 0.885.
    assert GBMEngine().jump_variance == pytest.approx(0.1278, abs=5e-4)


def test_jump_variance_scales_with_the_tick_size() -> None:
    # Fewer, larger ticks means fewer jump draws per year — the budget must follow, or a
    # coarser tick would quietly realise less than its target volatility.
    fine, coarse = GBMEngine(tick_seconds=0.5), GBMEngine(tick_seconds=5.0)
    assert coarse.jump_variance == pytest.approx(fine.jump_variance / 10.0)


def test_realised_volatility_lands_near_target() -> None:
    # ±15% keeps RNG variance from flaking the test; practice is ~3% (MARKET_SIMULATOR §5).
    measured = measure_annualised_sigma(
        "MU", steps=VOL_STEPS, sample_every=VOL_SAMPLE_EVERY, seed=7
    )
    assert measured == pytest.approx(0.885, rel=0.15)


def test_prices_stay_positive_and_finite() -> None:
    engine = GBMEngine(seed=11)
    for ticker in SEED_WATCHLIST:
        engine.add_ticker(ticker)
    for _ in range(10_000):
        for price in engine.step().values():
            assert price > 0.0 and math.isfinite(price)


def _measure_rho(monkeypatch: pytest.MonkeyPatch, a: str, b: str, steps: int = 20_000) -> float:
    """Correlation of tick log-returns with jumps switched off.

    Jumps are drawn independently per ticker, so leaving them on would dilute the measured
    correlation by the jump share of variance — 27% for a semicap name — and the test would
    be asserting the volatility budget, not the Cholesky. Zeroing JUMP_PROB before
    construction also zeroes `jump_variance`, so sigma_d becomes the full target sigma.
    """
    monkeypatch.setattr(simulator, "JUMP_PROB", 0.0)
    engine = GBMEngine(seed=5)
    engine.add_ticker(a)
    engine.add_ticker(b)
    paths: dict[str, list[float]] = {a: [], b: []}
    for _ in range(steps):
        prices = engine.step()
        for ticker, series in paths.items():
            series.append(prices[ticker])
    return statistics.correlation(_log_returns(paths[a]), _log_returns(paths[b]))


def test_correlation_recovers_the_sector_block(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _measure_rho(monkeypatch, "LRCX", "AMAT") == pytest.approx(0.90, abs=0.05)


def test_correlation_recovers_a_near_independent_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _measure_rho(monkeypatch, "AMD", "PLTR") == pytest.approx(0.15, abs=0.05)


def test_sector_rho_lookup() -> None:
    assert sector_rho("LRCX", "AMAT") == 0.90        # same block
    assert sector_rho("MU", "LRCX") == 0.65          # cross block, either order
    assert sector_rho("LRCX", "MU") == 0.65
    assert sector_rho("PLTR", "MU") == 0.15          # wildcard
    assert sector_rho("SLV", "AMAT") == 0.25
    assert sector_rho("ZZZZ", "MU") == 0.35          # unknown -> DEFAULT_RHO
    assert sector_rho("ZZZZ", "YYYY") == 0.35        # both unknown, same "other" sector


def test_correlation_matrix_is_positive_definite_without_the_ridge() -> None:
    # This is the test that catches a bad SECTOR_RHO edit — in CI, not in the demo.
    cases = [
        list(SEED_WATCHLIST),
        ["LRCX", "AMAT"] * 12,
        [f"X{i}" for i in range(50)],
        ["MU", "AMD", "INTC", "MRVL", "ALAB"],
        ["LRCX", "AMAT"],
    ]
    for tickers in cases:
        assert _cholesky(correlation_matrix(tickers)) is not None


def test_seeded_runs_are_bit_reproducible() -> None:
    a, b = GBMEngine(seed=42), GBMEngine(seed=42)
    for engine in (a, b):
        engine.add_ticker("MU", 877.57)
    assert [a.step() for _ in range(100)] == [b.step() for _ in range(100)]


def test_different_seeds_diverge() -> None:
    a, b = GBMEngine(seed=1), GBMEngine(seed=2)
    for engine in (a, b):
        engine.add_ticker("MU", 877.57)
    assert a.step() != b.step()


def test_anchoring_overrides_the_seed_table() -> None:
    engine = GBMEngine(seed=42)
    engine.add_ticker("MU", start_price=1234.5)
    assert engine.price("MU") == 1234.5             # ignores SEED_PRICES["MU"]


def test_unknown_ticker_gets_a_plausible_level() -> None:
    engine = GBMEngine(seed=42)
    engine.add_ticker("ZZZZ")
    price = engine.price("ZZZZ")
    assert price is not None and 40.0 <= price <= 400.0


def test_membership_churn_preserves_survivors() -> None:
    engine = GBMEngine(seed=3)
    for ticker in ("MU", "AMD", "SLV"):
        engine.add_ticker(ticker)
    engine.step()
    amd = engine.price("AMD")

    engine.remove_ticker("MU")
    assert engine.price("MU") is None
    assert engine.price("AMD") == amd               # untouched by the removal
    assert set(engine.step()) == {"AMD", "SLV"}

    engine.remove_ticker("MISSING")                 # idempotent
    engine.remove_ticker("AMD")
    assert set(engine.step()) == {"SLV"}            # n = 1: no Cholesky, no crash
    engine.remove_ticker("SLV")
    assert engine.step() == {}                      # n = 0


def test_set_price_only_moves_a_known_ticker() -> None:
    engine = GBMEngine(seed=3)
    engine.add_ticker("MU", 877.57)
    engine.set_price("MU", 900.0)
    assert engine.price("MU") == 900.0
    engine.set_price("NOPE", 1.0)                   # silently ignored
    assert engine.price("NOPE") is None


def test_jumps_stay_within_their_magnitude_bound() -> None:
    # Guards against a future "optimisation" that makes the shock additive or unbounded.
    engine = GBMEngine(seed=9)
    engine.add_ticker("MU", 877.57)
    previous = 877.57
    for _ in range(50_000):
        price = engine.step()["MU"]
        assert abs(math.log(price / previous)) < JUMP_MAX * 3
        previous = price
