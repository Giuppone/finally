"""Risk statistics, the optimisers and the trade builder (PORTFOLIO_ANALYTICS.md §9).

Closed-form checks wherever one exists: a golden-file test tells you the number changed,
an analytic one tells you the maths is wrong.
"""

from __future__ import annotations

import math

import pytest

from app.analytics import estimates, optimize, rebalance, risk


def cov_2x2(s1: float, s2: float, rho: float) -> list[list[float]]:
    return [[s1 * s1, rho * s1 * s2], [rho * s1 * s2, s2 * s2]]


def equicorrelated(n: int, sigma: float, rho: float) -> list[list[float]]:
    return [[sigma * sigma if i == j else rho * sigma * sigma for j in range(n)]
            for i in range(n)]


# ---- risk statistics ---------------------------------------------------------

def test_risk_contributions_sum_to_volatility() -> None:
    """Euler decomposition: sum(RC_i) == sigma_p exactly. If this drifts, "risk share" is
    no longer a decomposition and the whole panel is decorative."""
    tickers = ["MU", "AMD", "SLV", "PLTR"]
    weights = [0.4, 0.3, 0.2, 0.1]
    stats = risk.portfolio_stats(tickers, weights)
    total = sum(row["risk_contribution"] for row in stats["positions"])
    assert total == pytest.approx(stats["volatility"], abs=1e-5)
    assert sum(row["risk_share"] for row in stats["positions"]) == pytest.approx(1.0, abs=1e-5)


def test_single_asset_volatility_is_its_own_sigma() -> None:
    stats = risk.portfolio_stats(["AMD"], [1.0])
    assert stats["volatility"] == pytest.approx(estimates.volatility("AMD"), abs=1e-9)
    assert stats["diversification_ratio"] == pytest.approx(1.0, abs=1e-6)
    assert stats["effective_n"] == pytest.approx(1.0, abs=1e-6)


def test_cash_lowers_volatility_without_being_renormalised_away() -> None:
    """Half in cash really is half the volatility - renormalising the risky sleeve to 1
    would report the book as though it were fully invested."""
    full = risk.portfolio_stats(["MU", "AMD"], [0.5, 0.5], cash_weight=0.0)
    half = risk.portfolio_stats(["MU", "AMD"], [0.25, 0.25], cash_weight=0.5)
    # 1e-6, not 1e-9: the wire values are rounded to six places on the way out.
    assert half["volatility"] == pytest.approx(full["volatility"] / 2, abs=1e-6)


def test_all_cash_reports_no_sharpe_rather_than_infinity() -> None:
    stats = risk.portfolio_stats(["MU"], [0.0], cash_weight=1.0)
    assert stats["volatility"] == 0.0
    assert stats["sharpe"] is None
    assert stats["expected_return"] == pytest.approx(estimates.RISK_FREE_RATE, abs=1e-9)


def test_var_needs_a_total_value() -> None:
    assert risk.portfolio_stats(["MU"], [1.0])["var_95_1d_parametric"] is None
    stats = risk.portfolio_stats(["MU"], [1.0], total_value=10_000.0)
    expected = 1.645 * estimates.volatility("MU") / math.sqrt(252) * 10_000.0
    assert stats["var_95_1d_parametric"] == pytest.approx(expected, abs=0.01)


def test_expected_return_is_labelled_as_damped() -> None:
    """The mu here is the simulator's capped drift, not a forecast. Never report it bare."""
    assert "damped" in risk.portfolio_stats(["MU"], [1.0])["expected_return_basis"]


# ---- optimisers --------------------------------------------------------------

def test_two_asset_min_variance_matches_the_closed_form() -> None:
    """w1 = (s2^2 - rho*s1*s2) / (s1^2 + s2^2 - 2*rho*s1*s2). The test that catches a wrong
    gradient or a broken projection."""
    s1, s2, rho = 0.2, 0.4, 0.3
    expected = (s2 ** 2 - rho * s1 * s2) / (s1 ** 2 + s2 ** 2 - 2 * rho * s1 * s2)
    weights = optimize.min_variance(cov_2x2(s1, s2, rho), cap=1.0)
    assert weights[0] == pytest.approx(expected, abs=1e-4)
    assert sum(weights) == pytest.approx(1.0, abs=1e-12)


def test_equal_sigma_equal_rho_gives_equal_weights() -> None:
    cov = equicorrelated(4, sigma=0.5, rho=0.4)
    assert optimize.min_variance(cov, cap=1.0) == pytest.approx([0.25] * 4, abs=1e-4)
    assert optimize.risk_parity(cov, cap=1.0) == pytest.approx([0.25] * 4, abs=1e-4)


def test_identical_assets_do_not_blow_up() -> None:
    """rho = 1 makes the covariance singular; the ridge in correlation_matrix and the
    clamp in quadratic_form both exist for this case."""
    cov = equicorrelated(3, sigma=0.3, rho=1.0)
    weights = optimize.min_variance(cov, cap=1.0)
    assert sum(weights) == pytest.approx(1.0, abs=1e-9)
    assert risk.portfolio_volatility(cov, weights) == pytest.approx(0.3, abs=1e-6)


def test_min_variance_prefers_the_quieter_asset() -> None:
    weights = optimize.min_variance(cov_2x2(0.15, 0.60, 0.2), cap=1.0)
    assert weights[0] > weights[1]


def test_risk_parity_equalises_risk_shares() -> None:
    cov = [[0.36, 0.06, 0.02], [0.06, 0.09, 0.01], [0.02, 0.01, 0.04]]
    weights = optimize.risk_parity(cov, cap=1.0)
    vol = risk.portfolio_volatility(cov, weights)
    sigma_w = risk.matvec(cov, weights)
    shares = [w * s / vol / vol for w, s in zip(weights, sigma_w)]
    assert shares == pytest.approx([1 / 3] * 3, abs=1e-4)


def test_risk_parity_is_not_equal_weight_when_vols_differ() -> None:
    """The thesis of the whole feature: equal money is not equal risk."""
    cov = [[0.36, 0.06, 0.02], [0.06, 0.09, 0.01], [0.02, 0.01, 0.04]]
    weights = optimize.risk_parity(cov, cap=1.0)
    assert max(abs(w - 1 / 3) for w in weights) > 0.05
    assert weights[0] < weights[2]              # loudest asset gets the smallest slice


def test_max_sharpe_beats_equal_weight_on_its_own_objective() -> None:
    tickers = ["MU", "AMD", "SLV", "PLTR"]
    cov = estimates.covariance(tickers)
    mu = estimates.drifts(tickers)
    rf = estimates.RISK_FREE_RATE

    def sharpe(w: list[float]) -> float:
        return (sum(a * b for a, b in zip(w, mu)) - rf) / risk.portfolio_volatility(cov, w)

    assert sharpe(optimize.max_sharpe(mu, cov, rf, cap=1.0)) >= sharpe([0.25] * 4)


def test_cap_binds_and_forces_equal_weights() -> None:
    cov = estimates.covariance(["MU", "AMD", "SLV", "PLTR"])
    assert optimize.min_variance(cov, cap=0.25) == pytest.approx([0.25] * 4, abs=1e-6)


def test_a_cap_below_one_over_n_is_infeasible_and_says_so() -> None:
    with pytest.raises(optimize.Infeasible) as excinfo:
        optimize.project([0.25] * 4, cap=0.2)
    assert "0.2000" in str(excinfo.value) and "1/4" in str(excinfo.value)


def test_projection_respects_both_bounds() -> None:
    weights = optimize.project([5.0, -3.0, 0.1, 0.2], cap=0.4)
    assert sum(weights) == pytest.approx(1.0, abs=1e-12)
    assert all(-1e-12 <= w <= 0.4 + 1e-9 for w in weights)


@pytest.mark.parametrize("objective", optimize.OBJECTIVES)
def test_optimisers_are_deterministic_and_feasible(objective: str) -> None:
    """The E2E asserts on these numbers, so the same request must give the same answer."""
    tickers = ["MU", "AMD", "SLV", "ALAB", "PLTR"]
    cov = estimates.covariance(tickers)
    mu = estimates.drifts(tickers)
    first = optimize.solve(objective, mu, cov, estimates.RISK_FREE_RATE, 0.35)
    second = optimize.solve(objective, mu, cov, estimates.RISK_FREE_RATE, 0.35)
    assert first == second
    assert sum(first) == pytest.approx(1.0, abs=1e-9)
    assert all(w <= 0.35 + 1e-9 for w in first)


def test_min_variance_never_increases_volatility() -> None:
    """The single most valuable assertion in this feature: whatever you hold, the suggestion
    is not worse on the objective it optimises."""
    tickers = ["ALAB", "MRVL", "MU", "AMD", "INTC", "SLV"]
    cov = estimates.covariance(tickers)
    lopsided = [0.62, 0.20, 0.10, 0.05, 0.02, 0.01]
    optimised = optimize.min_variance(cov, cap=0.35)
    assert risk.portfolio_volatility(cov, optimised) <= risk.portfolio_volatility(cov, lopsided)


def test_floor_drops_slivers_and_renormalises() -> None:
    weights = optimize.apply_floor([0.6, 0.395, 0.005], floor=0.01, cap=1.0)
    assert weights[2] == 0.0
    assert sum(weights) == pytest.approx(1.0, abs=1e-12)


def test_floor_that_would_empty_the_portfolio_is_ignored() -> None:
    weights = optimize.apply_floor([0.25] * 4, floor=0.5, cap=1.0)
    assert weights == pytest.approx([0.25] * 4)


# ---- trade construction ------------------------------------------------------

def test_plan_sells_before_buys() -> None:
    _, trades, _ = rebalance.build_plan(
        targets={"MU": 0.5, "AMD": 0.5},
        current_values={"MU": 1_000.0, "AMD": 9_000.0},
        prices={"MU": 100.0, "AMD": 50.0},
        cash=0.0, cash_reserve=0.0,
    )
    sides = [leg["side"] for leg in trades]
    assert sides == sorted(sides, key=lambda s: 0 if s == "sell" else 1)
    assert sides[0] == "sell"


def test_trades_reconstruct_the_target_weights() -> None:
    targets = {"MU": 0.5, "AMD": 0.3, "SLV": 0.2}
    prices = {"MU": 100.0, "AMD": 50.0, "SLV": 25.0}
    current = {"MU": 1_000.0, "AMD": 200.0, "SLV": 300.0}
    _, trades, _ = rebalance.build_plan(
        targets=targets, current_values=current, prices=prices,
        cash=8_500.0, cash_reserve=0.0,
    )
    final = dict(current)
    for leg in trades:
        signed = leg["notional"] * (1 if leg["side"] == "buy" else -1)
        final[leg["ticker"]] = final.get(leg["ticker"], 0.0) + signed
    invested = sum(final.values())
    for ticker, target in targets.items():
        assert final[ticker] / invested == pytest.approx(target, abs=2e-3)


def test_dust_legs_are_not_traded() -> None:
    """A $5 drift is not worth a blotter row."""
    _, trades, _ = rebalance.build_plan(
        targets={"MU": 0.5, "AMD": 0.5},
        current_values={"MU": 5_005.0, "AMD": 4_995.0},
        prices={"MU": 100.0, "AMD": 50.0},
        cash=0.0, cash_reserve=0.0,
    )
    assert trades == []


def test_a_buy_is_clamped_to_the_cash_available() -> None:
    """A buy funded by a sell that cannot happen must shrink, not fail.

    GHOST is worth $5,000 of the book but has no price, so the plan counts it toward
    investable value and then cannot sell it. Without the clamp the MU buy is sized against
    money that never arrives and the whole rebalance dies on its last leg.
    """
    _, trades, warnings = rebalance.build_plan(
        targets={"MU": 1.0},
        current_values={"MU": 100.0, "GHOST": 5_000.0},
        prices={"MU": 100.0},
        cash=3_000.0, cash_reserve=0.0,
    )
    buy = next(leg for leg in trades if leg["ticker"] == "MU")
    assert buy["clamped"] is True
    assert buy["quantity"] == 30.0                      # $3,000 of cash, not the $8,000 target
    assert buy["notional"] <= 3_000.0
    assert any("clamped" in warning for warning in warnings)


def test_an_unpriced_position_is_reported_not_traded() -> None:
    _, trades, warnings = rebalance.build_plan(
        targets={"MU": 1.0},
        current_values={"MU": 100.0, "GHOST": 5_000.0},
        prices={"MU": 100.0},
        cash=0.0, cash_reserve=0.0,
    )
    assert all(leg["ticker"] != "GHOST" for leg in trades)
    assert any("GHOST" in warning for warning in warnings)


def test_a_position_outside_the_targets_is_sold_down() -> None:
    """"Rebalance into these names" has to mean something for the names left out."""
    rows, trades, _ = rebalance.build_plan(
        targets={"MU": 1.0},
        current_values={"MU": 5_000.0, "AMD": 5_000.0},
        prices={"MU": 100.0, "AMD": 50.0},
        cash=0.0, cash_reserve=0.0,
    )
    amd = next(row for row in rows if row["ticker"] == "AMD")
    assert amd["target_weight"] == 0.0
    assert any(leg["ticker"] == "AMD" and leg["side"] == "sell" for leg in trades)


def test_cash_reserve_is_held_back() -> None:
    rows, _, _ = rebalance.build_plan(
        targets={"MU": 1.0}, current_values={"MU": 10_000.0},
        prices={"MU": 100.0}, cash=0.0, cash_reserve=2_000.0,
    )
    assert rows[0]["target_value"] == pytest.approx(8_000.0, abs=0.01)


def test_current_weights_of_an_empty_book_is_empty() -> None:
    assert rebalance.current_weights({}) == {}


def test_a_full_exit_sells_the_exact_held_quantity() -> None:
    """0.4537 held, truncated delta says sell 0.4536, and 0.0001 shares survives as a
    phantom row in the positions table (Review.md B11). Exiting sells all of it."""
    _, trades, _ = rebalance.build_plan(
        targets={"AMD": 1.0},
        current_values={"AMD": 5_000.0, "ALAB": 151.89},
        prices={"AMD": 50.0, "ALAB": 334.78},
        cash=0.0, cash_reserve=0.0,
        current_quantities={"AMD": 100.0, "ALAB": 0.4537},
    )
    exit_leg = next(leg for leg in trades if leg["ticker"] == "ALAB")
    assert exit_leg["side"] == "sell"
    assert exit_leg["quantity"] == 0.4537          # not the truncated 0.4536


def test_a_partial_sell_is_still_truncated() -> None:
    """Only a full exit takes the exact quantity; trimming a position must stay truncated,
    or the plan can ask for a hair more than the price supports."""
    _, trades, _ = rebalance.build_plan(
        targets={"MU": 0.5, "AMD": 0.5},
        current_values={"MU": 9_000.0, "AMD": 1_000.0},
        prices={"MU": 100.0, "AMD": 50.0},
        cash=0.0, cash_reserve=0.0,
        current_quantities={"MU": 90.0, "AMD": 20.0},
    )
    sell = next(leg for leg in trades if leg["side"] == "sell")
    assert sell["ticker"] == "MU" and sell["quantity"] == 40.0
