"""Where mu and Sigma come from.

The risk model reads `market/seeds.py` and reuses `market.simulator.correlation_matrix`
**by import, not by copy** (PORTFOLIO_ANALYTICS.md §2). Two reasons, and the second is the
one that matters:

* `correlation_matrix` already resolves the sector blocks, the `"*"` wildcards and the
  `_ridge()` PSD repair that a bad `SECTOR_RHO` edit would otherwise turn into a crash.
* In `SIMULATED` and `ANCHORED` - the two modes this project actually runs in - prices ARE
  GBM paths with exactly these sigmas and these correlations. So the analytics is not an
  estimate of the world the user trades in; it is that world's own parameters. A second
  copy of this table could drift, and the risk panel would then describe a different market
  than the one printing the prices.

The mu here is the simulator's DAMPED drift (~10% of realised, capped at 0.20), which is
the true drift of the simulated process but NOT a forecast of the real stock
(MARKET_SIMULATOR.md §6). Every response that reports an expected return carries
`EXPECTED_RETURN_BASIS` beside it, and the default rebalance objectives use only Sigma.
"""

from __future__ import annotations

from ..market.seeds import (
    CALIBRATION_WINDOW,
    DEFAULT_PARAMS,
    TICKER_CAGR,
    TICKER_PARAMS,
)
from ..market.simulator import correlation_matrix

# Annual, decimal. The Sharpe denominator and the return on the cash sleeve. Reported in
# every response so a reader can re-derive the ratio rather than guess at it.
RISK_FREE_RATE = 0.04
TRADING_DAYS = 252
VAR_Z_95 = 1.645

EXPECTED_RETURN_BASIS = "simulator-calibrated (damped drift)"


def drift(ticker: str) -> float:
    return TICKER_PARAMS.get(ticker, DEFAULT_PARAMS)["mu"]


def volatility(ticker: str) -> float:
    """Annualised sigma. This is TOTAL volatility including the simulator's jump component
    - `diffusion_sigma()` splits it back out, so the table's value is what the path
    actually realises."""
    return TICKER_PARAMS.get(ticker, DEFAULT_PARAMS)["sigma"]


def drifts(tickers: list[str]) -> list[float]:
    return [drift(ticker) for ticker in tickers]


def volatilities(tickers: list[str]) -> list[float]:
    return [volatility(ticker) for ticker in tickers]


def covariance(tickers: list[str]) -> list[list[float]]:
    """Sigma_ij = rho_ij * sigma_i * sigma_j, with Sigma_ii = sigma_i**2."""
    rho = correlation_matrix(tickers)
    sigma = volatilities(tickers)
    return [
        [rho[i][j] * sigma[i] * sigma[j] for j in range(len(tickers))]
        for i in range(len(tickers))
    ]


def cagr(ticker: str) -> float | None:
    """Realised compound annual growth over the calibration window, or None if never
    measured. DISPLAY ONLY - it is shown beside the damped drift so the damping is
    auditable, and it must never reach the simulator or the optimisers.

    The two differ by more than people expect: MU's damped drift is 0.20 against a measured
    CAGR of 6.39, because CAGR = exp(log-drift) - 1 and this window covers a melt-up.
    """
    return TICKER_CAGR.get(ticker)


def window() -> dict:
    """The window every sigma, mu and correlation was measured over. Surfaced in the risk
    panel so a reader can judge how old the model is without reading the source."""
    return dict(CALIBRATION_WINDOW)


def is_known(ticker: str) -> bool:
    """False when the ticker falls back to DEFAULT_PARAMS - worth telling the user, since
    a generic sigma=0.45 is a placeholder, not a measurement."""
    return ticker in TICKER_PARAMS
