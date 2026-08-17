"""Portfolio risk and return statistics.

Pure arithmetic over a weight vector and a covariance matrix - no I/O, no database, no
service. Everything is O(n^2) on n <= ~30 tickers, which is why there is no numpy here: it
would be the largest dependency in the tree for a 30x30 matrix-vector product.
"""

from __future__ import annotations

import math

import asyncio

from ..market.simulator import correlation_matrix
from . import estimates, optimize

# Enough to read as a smooth curve at the size the panel renders it; every extra point is
# another warm-started solve.
FRONTIER_POINTS = 32

_FRONTIER_CACHE: dict[tuple[tuple[str, ...], float, int],
                      tuple[tuple[float, float], ...]] = {}


async def frontier_for(tickers: tuple[str, ...], cap: float = 1.0,
                       points: int = FRONTIER_POINTS) -> tuple[tuple[float, float], ...]:
    """The frontier for a ticker set, memoised, computed without blocking the event loop.

    **Not `asyncio.to_thread`.** Tracing the frontier is ~48 solves of pure Python, which
    holds the GIL throughout - handing it to a worker thread relocates the stall without
    removing it, and the price stream stutters exactly as much. That was measured: the E2E
    suite went from 47s to over four minutes, with specs that never touch analytics slowing
    down too. Yielding between solves is what actually works; each one is ~12ms, invisible
    against a 500ms tick.

    Cached indefinitely because it depends on nothing that moves: sigma, mu and the
    correlations all come from `seeds.py`, which is a data file. Prices tick, the frontier
    does not - only the marker showing where the portfolio sits on it does.
    """
    key = (tickers, cap, points)
    cached = _FRONTIER_CACHE.get(key)
    if cached is not None:
        return cached

    cov = estimates.covariance(list(tickers))
    mu = estimates.drifts(list(tickers))
    curve: list[tuple[float, float]] = []
    for point in optimize.frontier_steps(mu, cov, cap, points):
        curve.append(point)
        await asyncio.sleep(0)

    result = tuple(optimize.efficient_only(curve))
    _FRONTIER_CACHE[key] = result
    return result


def matvec(matrix: list[list[float]], vector: list[float]) -> list[float]:
    return [sum(row[j] * vector[j] for j in range(len(vector))) for row in matrix]


def quadratic_form(matrix: list[list[float]], vector: list[float]) -> float:
    """w' M w. Clamped at zero: a covariance built from a ridged correlation matrix can
    still land a hair below zero on a degenerate weight vector, and sqrt() of -1e-18 is a
    crash rather than a risk number."""
    return max(sum(vector[i] * value for i, value in enumerate(matvec(matrix, vector))), 0.0)


def portfolio_volatility(cov: list[list[float]], weights: list[float]) -> float:
    return math.sqrt(quadratic_form(cov, weights))


def portfolio_stats(
    tickers: list[str],
    weights: list[float],
    *,
    cash_weight: float = 0.0,
    total_value: float | None = None,
) -> dict:
    """The full Risk & Return payload for one weight vector.

    `weights` are portfolio weights of the RISKY sleeve; `cash_weight` is what is left in
    cash. Cash is modelled as a genuine risk-free asset (sigma=0, mu=r_f, zero covariance),
    not dropped: a 60%-cash book really is lower-volatility, and renormalising the risky
    weights to 1 would report it as though it were fully invested.
    """
    cov = estimates.covariance(tickers)
    mu = estimates.drifts(tickers)
    sigma = estimates.volatilities(tickers)
    rf = estimates.RISK_FREE_RATE

    expected_return = sum(w * m for w, m in zip(weights, mu)) + cash_weight * rf
    vol = portfolio_volatility(cov, weights)

    # Euler decomposition: RC_i = w_i * (Sigma w)_i / sigma_p, and sum(RC_i) == sigma_p
    # exactly. That identity is what makes "risk share" a real decomposition rather than a
    # heuristic, and the unit tests assert it.
    sigma_w = matvec(cov, weights)
    if vol > 0:
        marginal = [value / vol for value in sigma_w]
        contributions = [w * m for w, m in zip(weights, marginal)]
    else:
        marginal = [0.0] * len(tickers)
        contributions = [0.0] * len(tickers)

    positions = [
        {
            "ticker": ticker,
            "weight": round(weights[i], 6),
            "expected_return": round(mu[i], 6),
            "volatility": round(sigma[i], 6),
            "marginal_risk": round(marginal[i], 6),
            "risk_contribution": round(contributions[i], 6),
            "risk_share": round(contributions[i] / vol, 6) if vol > 0 else 0.0,
            "calibrated": estimates.is_known(ticker),
        }
        for i, ticker in enumerate(tickers)
    ]

    weighted_sigma = sum(w * s for w, s in zip(weights, sigma))
    risky = sum(weights)

    return {
        "expected_return": round(expected_return, 6),
        "volatility": round(vol, 6),
        # None, never inf: an all-cash book has no risk to be compensated for, and a JSON
        # Infinity is not valid JSON anyway.
        "sharpe": round((expected_return - rf) / vol, 4) if vol > 0 else None,
        "var_95_1d_parametric": (
            round(estimates.VAR_Z_95 * vol / math.sqrt(estimates.TRADING_DAYS) * total_value, 2)
            if total_value else None
        ),
        # 1.0 = no diversification benefit at all (one name, or a set that moves as one).
        "diversification_ratio": round(weighted_sigma / vol, 4) if vol > 0 else None,
        # Inverse HHI over the risky sleeve NORMALISED to 1: "how many effective positions".
        # Computed on raw weights instead, a half-cash two-stock book would report 4.
        "effective_n": (
            round(risky ** 2 / sum(w * w for w in weights), 3)
            if risky > 0 and sum(w * w for w in weights) > 0 else 0.0
        ),
        "cash_weight": round(cash_weight, 6),
        "risk_free_rate": rf,
        "expected_return_basis": estimates.EXPECTED_RETURN_BASIS,
        "positions": positions,
        "correlations": {
            "tickers": list(tickers),
            "matrix": [[round(value, 4) for value in row]
                       for row in correlation_matrix(tickers)],
        },
    }
