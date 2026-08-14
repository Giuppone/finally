"""Correlated jump-diffusion price simulation. Derivations in MARKET_SIMULATOR.md."""

from __future__ import annotations

import math
import random
import time

from .models import Tick
from .seeds import (
    DEFAULT_PARAMS,
    DEFAULT_RHO,
    FALLBACK_PRICE_RANGE,
    SECTOR_RHO,
    SECTORS,
    SEED_PRICES,
    TICKER_PARAMS,
)
from .source import MarketDataSource

SECONDS_PER_TRADING_YEAR = 252 * 6.5 * 3600          # 5,896,800
TICK_SECONDS = 0.5

JUMP_PROB = 1e-4                                     # ≈ 4.7 events/ticker/trading day
JUMP_MIN, JUMP_MAX = 0.005, 0.015
_JUMP_E_SQ = (JUMP_MIN**2 + JUMP_MIN * JUMP_MAX + JUMP_MAX**2) / 3.0


def diffusion_sigma(target_sigma: float, jump_variance: float) -> float:
    """Diffusion vol such that diffusion + jumps realise `target_sigma`.

    The archived design added jumps ON TOP of the target, producing 392% annualised vol
    from the jump term alone — 16x the intent. Subtracting is the fix. MARKET_SIMULATOR.md
    §5 has the arithmetic and the Monte-Carlo check (±3%).
    """
    return math.sqrt(max(target_sigma**2 - jump_variance, 1e-6))


class GBMEngine:
    """Correlated jump-diffusion price paths. Synchronous, pure stdlib, no I/O."""

    def __init__(self, seed: int | None = None, tick_seconds: float = TICK_SECONDS) -> None:
        # Instance-local RNG: tests cannot be perturbed by other code drawing randomness.
        self._rng = random.Random(seed)
        self._dt = tick_seconds / SECONDS_PER_TRADING_YEAR
        self._sqrt_dt = math.sqrt(self._dt)
        self._jump_variance = JUMP_PROB * _JUMP_E_SQ * (SECONDS_PER_TRADING_YEAR / tick_seconds)

        self._tickers: list[str] = []
        self._price: dict[str, float] = {}
        self._drift: dict[str, float] = {}           # (mu - sigma_d^2/2) * dt, precomputed
        self._vol: dict[str, float] = {}             # sigma_d * sqrt(dt), precomputed
        self._chol: list[list[float]] | None = None

    @property
    def jump_variance(self) -> float:
        return self._jump_variance

    # ---- membership --------------------------------------------------
    def add_ticker(self, ticker: str, start_price: float | None = None) -> None:
        if ticker in self._price:
            return
        params = TICKER_PARAMS.get(ticker, DEFAULT_PARAMS)
        sigma_d = diffusion_sigma(params["sigma"], self._jump_variance)
        self._tickers.append(ticker)
        self._price[ticker] = self._starting_price(ticker, start_price)
        self._drift[ticker] = (params["mu"] - 0.5 * sigma_d**2) * self._dt
        self._vol[ticker] = sigma_d * self._sqrt_dt
        self._rebuild_cholesky()

    def _starting_price(self, ticker: str, start_price: float | None) -> float:
        if start_price is not None and start_price > 0:
            return start_price
        seeded = SEED_PRICES.get(ticker)
        if seeded:
            return seeded
        return self._rng.uniform(*FALLBACK_PRICE_RANGE)

    def remove_ticker(self, ticker: str) -> None:
        if ticker not in self._price:
            return
        self._tickers.remove(ticker)
        for store in (self._price, self._drift, self._vol):
            store.pop(ticker, None)
        self._rebuild_cholesky()

    def set_price(self, ticker: str, price: float) -> None:
        """Jump a path to a new level — used at the ANCHORED session roll (design §9.3)."""
        if ticker in self._price:
            self._price[ticker] = price

    def price(self, ticker: str) -> float | None:
        return self._price.get(ticker)

    # ---- the step ----------------------------------------------------
    def step(self) -> dict[str, float]:
        """Advance one tick. Returns {ticker: price} at full precision."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z_ind = [self._rng.gauss(0.0, 1.0) for _ in range(n)]
        if self._chol is None:
            z = z_ind
        else:
            # L is lower-triangular, so only k <= i contribute — half a dense multiply.
            z = [sum(self._chol[i][k] * z_ind[k] for k in range(i + 1)) for i in range(n)]

        out: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            price = self._price[ticker] * math.exp(self._drift[ticker] + self._vol[ticker] * z[i])
            if self._rng.random() < JUMP_PROB:
                shock = self._rng.uniform(JUMP_MIN, JUMP_MAX)
                price *= 1.0 + (shock if self._rng.random() < 0.5 else -shock)
            self._price[ticker] = price
            out[ticker] = price
        return out

    # ---- correlation --------------------------------------------------
    def _rebuild_cholesky(self) -> None:
        n = len(self._tickers)
        if n <= 1:
            self._chol = None
            return
        matrix = correlation_matrix(self._tickers)
        self._chol = _cholesky(matrix) or _cholesky(_ridge(matrix, 0.05))


def correlation_matrix(tickers: list[str]) -> list[list[float]]:
    return [
        [1.0 if i == j else sector_rho(a, b) for j, b in enumerate(tickers)]
        for i, a in enumerate(tickers)
    ]


def sector_rho(a: str, b: str) -> float:
    sa, sb = SECTORS.get(a, "other"), SECTORS.get(b, "other")
    if sa == sb:
        return SECTOR_RHO.get((sa, sa), DEFAULT_RHO)
    for x, y in ((sa, sb), (sb, sa)):
        if (x, y) in SECTOR_RHO:
            return SECTOR_RHO[(x, y)]
        if (x, "*") in SECTOR_RHO:
            return SECTOR_RHO[(x, "*")]
    return DEFAULT_RHO


def _cholesky(m: list[list[float]]) -> list[list[float]] | None:
    """Lower-triangular Cholesky factor, or None if not positive-definite."""
    n = len(m)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = sum(L[i][k] * L[j][k] for k in range(j))
            if i == j:
                d = m[i][i] - s
                if d <= 1e-12:
                    return None
                L[i][j] = math.sqrt(d)
            else:
                L[i][j] = (m[i][j] - s) / L[j][j]
    return L


def _ridge(m: list[list[float]], eps: float) -> list[list[float]]:
    """Shrink toward the identity to restore positive-definiteness."""
    n = len(m)
    return [
        [m[i][j] * (1 - eps) + (eps if i == j else 0.0) for j in range(n)]
        for i in range(n)
    ]


class SimulatedSource(MarketDataSource):
    """Drives the GBM engine. Used by BOTH SIMULATED and ANCHORED modes — the only
    difference between those two is which AnchorProvider fills `anchors`, which is why
    ANCHORED costs almost no extra code."""

    def __init__(self, engine: GBMEngine, poll_interval: float = TICK_SECONDS) -> None:
        self._engine = engine
        self.poll_interval = poll_interval

    async def prime(self, tickers: list[str], anchors: dict[str, float]) -> None:
        for ticker in tickers:
            self._engine.add_ticker(ticker, start_price=anchors.get(ticker))

    async def poll(self, tickers: list[str]) -> list[Tick]:
        now = time.time()
        wanted = set(tickers)
        return [Tick(t, p, now) for t, p in self._engine.step().items() if t in wanted]

    async def release(self, ticker: str) -> None:
        self._engine.remove_ticker(ticker)

    async def rebase(self, ticker: str, price: float) -> None:
        self._engine.set_price(ticker, price)


class StaticAnchorProvider:
    """SIMULATED mode: the seed table, no network. Unknown tickers get a plausible level."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()

    async def anchors(self, tickers: list[str], session_date: str = "") -> dict[str, float]:
        return {
            t: SEED_PRICES.get(t) or self._rng.uniform(*FALLBACK_PRICE_RANGE)
            for t in tickers
        }

    async def is_known(self, ticker: str, session_date: str = "") -> bool:
        return True          # no universe to check against; the regex is the only gate

    async def refresh(self, session_date: str) -> None:
        return None
