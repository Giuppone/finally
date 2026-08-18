"""GBM maths — design §14.5 and MARKET_SIMULATOR.md §11."""

from __future__ import annotations

import math
import statistics

import pytest

from app.market import simulator
from app.market.seeds import MEASURED_RHO, SEED_WATCHLIST, TICKER_PARAMS
from app.market.simulator import (
    JUMP_MAX,
    SECONDS_PER_TRADING_YEAR,
    GBMEngine,
    _cholesky,
    _factor,
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


def test_measured_correlations_win_over_the_blocks() -> None:
    """A calibrated pair uses its measured value, in either order. The blocks were always a
    stand-in for a measurement; where one exists it should be used."""
    measured = MEASURED_RHO[("AMAT", "LRCX")]
    assert sector_rho("AMAT", "LRCX") == measured
    assert sector_rho("LRCX", "AMAT") == measured
    assert measured != 0.90                          # not the semicap block value


def test_a_ticker_correlates_perfectly_with_itself() -> None:
    assert sector_rho("MU", "MU") == 1.0


def test_the_blocks_still_cover_an_uncalibrated_ticker() -> None:
    """This is what lets the model price a name the user adds before anyone calibrates it -
    the reason MARKET_SIMULATOR.md §4 encodes blocks rather than a frozen matrix."""
    assert sector_rho("PLTR", "COIN") == 0.15        # software wildcard
    assert sector_rho("SLV", "COIN") == 0.25         # commodity wildcard
    assert sector_rho("LRCX", "COIN") == 0.35        # no rule -> DEFAULT_RHO
    assert sector_rho("ZZZZ", "YYYY") == 0.35        # both unknown, same "other" sector


def test_correlation_matrix_is_positive_definite_without_the_ridge() -> None:
    """Catches a bad SECTOR_RHO or MEASURED_RHO edit in CI rather than in the demo.

    Every case is a set of DISTINCT tickers, which is the only kind the tracked set can
    ever hold. The whole calibrated universe is included: a measured matrix has no reason
    to be positive definite a priori, and a recalibration that produced one that is not
    would otherwise only surface as a silent fall through to the ridge.
    """
    cases = [
        list(SEED_WATCHLIST),
        sorted(TICKER_PARAMS),                       # every calibrated ticker
        [f"X{i}" for i in range(50)],                # all unknown -> flat DEFAULT_RHO
        ["MU", "AMD", "INTC", "MRVL", "ALAB"],
        ["LRCX", "AMAT"],
        ["LRCX", "AMAT", "SMH", "ASML"],             # the 0.85-0.91 cluster
    ]
    for tickers in cases:
        assert _cholesky(correlation_matrix(tickers)) is not None, tickers


def test_repeated_tickers_fall_back_to_the_ridge() -> None:
    """A ticker listed twice is perfectly correlated with itself, so the raw matrix is
    singular by construction - correct maths, not a bad edit. `_factor` must still return
    a usable factor, because a crash in the price loop is the one outcome that is not
    survivable.
    """
    duplicated = ["LRCX", "AMAT"] * 12
    assert _cholesky(correlation_matrix(duplicated)) is None      # genuinely singular
    assert _factor(duplicated) is not None                        # ridge rescues it


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


# ---- subset stepping (Market_data_review.md P2) ------------------------------

def test_stepping_a_subset_leaves_the_other_paths_untouched() -> None:
    """add_ticker()'s single-ticker poll must not move every other tracked path.

    Against the pre-fix engine (step() advanced everything and poll() merely filtered the
    result) AMD drifts here even though only MU was asked for.
    """
    engine = GBMEngine(seed=42)
    for ticker in ("MU", "AMD", "SLV"):
        engine.add_ticker(ticker)

    untouched = {t: engine.price(t) for t in ("AMD", "SLV")}
    moved = engine.price("MU")

    out = engine.step(["MU"])

    assert set(out) == {"MU"}
    assert engine.price("MU") != moved
    for ticker, price in untouched.items():
        assert engine.price(ticker) == price


def test_stepping_an_empty_subset_is_a_no_op() -> None:
    engine = GBMEngine(seed=42)
    engine.add_ticker("MU")
    before = engine.price("MU")
    assert engine.step([]) == {}
    assert engine.price("MU") == before


def test_a_subset_step_still_correlates_within_the_subset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subset gets its own Cholesky factor, so the 0.90 LRCX/AMAT semicap block must
    survive being stepped without the rest of the basket. Jumps off for the same reason
    `_measure_rho` turns them off — they are drawn per ticker and dilute the measurement.
    """
    monkeypatch.setattr(simulator, "JUMP_PROB", 0.0)
    engine = GBMEngine(seed=7)
    for ticker in ("LRCX", "AMAT", "PLTR", "SLV"):
        engine.add_ticker(ticker)

    paths: dict[str, list[float]] = {"LRCX": [], "AMAT": []}
    for _ in range(20_000):
        prices = engine.step(["LRCX", "AMAT"])
        for ticker, series in paths.items():
            series.append(prices[ticker])

    measured = statistics.correlation(_log_returns(paths["LRCX"]), _log_returns(paths["AMAT"]))
    assert measured == pytest.approx(0.90, abs=0.05)
