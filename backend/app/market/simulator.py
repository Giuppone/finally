"""Correlated jump-diffusion price simulation. Derivations in MARKET_SIMULATOR.md."""

from __future__ import annotations

import math
import random
import time
from collections.abc import Iterable

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
    def step(self, tickers: Iterable[str] | None = None) -> dict[str, float]:
        """Advance one tick. Returns {ticker: price} at full precision.

        `None` advances every registered path — the steady-state loop, and the fast path
        since the cached Cholesky factor applies as-is.

        A strict subset advances ONLY those paths. This exists because `poll()` used to
        step everything and merely *filter* the returned list, so `add_ticker()`'s
        single-ticker poll silently moved every other tracked ticker one unscheduled step:
        its new price was written to the engine but never emitted as a Tick, so the next
        regular poll reported two compounded steps of variance as one. That broke the
        SIM_SEED bit-reproducibility guarantee (D13) in exactly the flow E2E replays — the
        LLM auto-adding a ticker mid-run (Market_data_review.md P2).
        """
        active = self._tickers
        if tickers is not None:
            wanted = set(tickers)
            active = [t for t in self._tickers if t in wanted]

        n = len(active)
        if n == 0:
            return {}

        # Full set -> the cached factor. A subset needs its own, since the correlation
        # structure of the whole basket does not apply to a slice of it.
        chol = self._chol if n == len(self._tickers) else _factor(active)

        z_ind = [self._rng.gauss(0.0, 1.0) for _ in range(n)]
        if chol is None:
            z = z_ind
        else:
            # L is lower-triangular, so only k <= i contribute — half a dense multiply.
            z = [sum(chol[i][k] * z_ind[k] for k in range(i + 1)) for i in range(n)]

        out: dict[str, float] = {}
        for i, ticker in enumerate(active):
            price = self._price[ticker] * math.exp(self._drift[ticker] + self._vol[ticker] * z[i])
            if self._rng.random() < JUMP_PROB:
                shock = self._rng.uniform(JUMP_MIN, JUMP_MAX)
                price *= 1.0 + (shock if self._rng.random() < 0.5 else -shock)
            self._price[ticker] = price
            out[ticker] = price
        return out

    # ---- correlation --------------------------------------------------
    def _rebuild_cholesky(self) -> None:
        self._chol = _factor(self._tickers)


def _factor(tickers: list[str]) -> list[list[float]] | None:
    """Cholesky factor for this ticker set, or None when there is nothing to correlate.

    Falls back to a ridge-shrunk matrix when the measured blocks are not positive-definite
    — which a bad SECTOR_RHO edit can cause, and which would otherwise crash the loop.
    """
    if len(tickers) <= 1:
        return None
    matrix = correlation_matrix(tickers)
    return _cholesky(matrix) or _cholesky(_ridge(matrix, 0.05))


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
        # Step ONLY what was asked for. Stepping everything and filtering the result moved
        # untracked-this-call paths without ever reporting them (Market_data_review.md P2).
        now = time.time()
        return [Tick(t, p, now) for t, p in self._engine.step(tickers).items()]

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
