# Market Simulator Design

Approach and code structure for generating realistic stock prices when Massive cannot supply live
data — which, on this project's free-tier key, is **always**
([MASSIVE_API.md §4](MASSIVE_API.md#4-entitlements--what-your-key-can-actually-call)).

The simulator is therefore not a fallback for the keyless case alone. It is the engine behind two of
the three modes in [MARKET_INTERFACE.md](MARKET_INTERFACE.md):

| Mode | Anchor prices | Intraday motion |
|---|---|---|
| `SIMULATED` | Static seed table (§3) | This engine |
| `ANCHORED` | **Real** closes from Massive | This engine |
| `LIVE` | Real | Massive polling — engine unused |

Every parameter below was calibrated against real market data pulled from Massive on 2026-08-10
(172 daily bars, 2025-12-01 → 2026-08-07). Reproduce with the script in §9.

> **Recalibrated 2026-08-17** over 2025-12-07 → 2026-08-14 (172 bars) across **26 tickers** — the
> seed ten plus sixteen imported from a real brokerage account. `scripts/calibrate_market.py` now
> exists and does this; §9 specified it long before it was written. The tables in §2 and §4 below
> are the *original* pull and are kept as the derivation; `seeds.py` is the live source of truth.
>
> Two findings from that run, both worth carrying into how the numbers are read:
>
> - **Volatility is stable; drift is not.** Shifting the window by one week moved every σ by under
>   2% (MU 0.885 → 0.883, SLV 0.749 → 0.749, AMAT 0.644 → 0.650) while ALAB's realised μ nearly
>   halved, 1.600 → 0.891. That instability *is* §6's argument for damping, measured rather than
>   asserted — and it is why the risk panel presents σ and correlation as measurements but μ as an
>   assumption.
> - **Some drifts are now negative** (META, MELI, NU, MP, PLTR). Only the upside is capped, so a
>   name that fell over the window drifts down in the simulator too — the two-sided market §6 was
>   trying to buy.
>
> Measured pairwise correlations (`MEASURED_RHO`) now take precedence over the §4 sector blocks,
> which remain the fallback for any pair involving an uncalibrated ticker. The blocks were blindest
> exactly where it mattered: SMH is a semiconductor ETF realising 0.85–0.90 against LRCX, AMAT and
> ASML, and no sector rule gave it anything but the 0.35 default — telling the optimiser that a fund
> holding the whole sector was an excellent hedge against that sector.
>
> Raw closes are cached in `backend/calibration/bars.json`, so adding a ticker costs one API call
> rather than twenty-six — which matters on a 5 req/min key.

---

## 1. Model: jump-diffusion GBM

Prices follow **geometric Brownian motion** with a **Poisson jump** overlay.

GBM is the right base: it is multiplicative, so prices can never go negative or hit zero; log-returns
are normal, matching the first-order behaviour of real equities; and it is the model underlying
Black-Scholes, so it is the one a finance-literate viewer expects.

Pure GBM alone looks *too smooth* — real tickers gap and lurch. The jump term supplies the drama
PLAN.md §6 asks for ("occasional random events — sudden moves on a ticker"). The critical design
point, and the one the archived draft got wrong, is that **jumps contribute variance too**, so they
must be budgeted against the target volatility rather than added on top (§5).

### Per-tick update

```
S ← S · exp( (μ − σ_d²/2)·dt  +  σ_d·√dt·Z )      # diffusion
if U < p_jump:  S ← S · (1 + J)                    # jump
```

| Symbol | Meaning |
|---|---|
| `μ` | Annualised drift (§6) |
| `σ_d` | Annualised **diffusion** volatility — *derived*, not the target (§5) |
| `dt` | Tick length as a fraction of a trading year |
| `Z` | Correlated standard normal (§7) |
| `p_jump` | Per-tick jump probability |
| `J` | Jump size, `±Uniform(0.005, 0.015)` |

### Deriving `dt`

At PLAN.md's 500 ms cadence, with 252 trading days of 6.5 hours:

```
seconds per trading year = 252 × 6.5 × 3600 = 5,896,800
dt                       = 0.5 / 5,896,800  = 8.4792e-08
ticks per trading year   = 5,896,800 / 0.5  = 11,793,600
```

Because the loop runs on wall-clock time, one simulated trading day elapses in 6.5 real hours — the
simulation advances at real-time pace, which is what makes the chart feel live rather than
fast-forwarded.

---

## 2. Calibration from real data

Pulled from Massive's Custom Bars endpoint for the ten seed tickers in PLAN.md §7. `σ` and `μ` are
annualised from daily log-returns.

| Ticker | Last close | σ (annual) | μ (annual, realised) | 8-month range |
|---|---:|---:|---:|---|
| ALAB | $334.17 | 1.060 | 1.600 | $100.27 – $483.02 |
| MRVL | $218.72 | 0.839 | 1.643 | $73.73 – $316.43 |
| MU | $877.57 | 0.885 | 2.299 | $225.52 – $1,213.56 |
| AMD | $483.36 | 0.720 | 1.421 | $190.95 – $580.91 |
| INTC | $101.65 | 0.835 | 1.722 | $36.05 – $140.94 |
| PLTR | $172.01 | 0.629 | 0.237 | $107.27 – $194.17 |
| ANET | $188.67 | 0.573 | 0.735 | $116.13 – $197.31 |
| LRCX | $311.35 | 0.692 | 1.270 | $154.79 – $433.33 |
| AMAT | $539.14 | 0.644 | 1.312 | $248.27 – $723.00 |
| SLV | $57.50 | 0.749 | 0.414 | $50.39 – $105.60 |

Two observations that should change how the simulator is written:

**These are not ordinary volatilities.** The seed watchlist is an AI-semiconductor basket in a
violent bull market — realised σ runs 0.57–1.06, where a typical large-cap sits near 0.20–0.30. The
archived draft's `sigma` values (0.17–0.50, built for AAPL/JPM/V) would render this watchlist
visibly *too calm*, roughly a third of its real motion.

**The archived seed prices are stale by orders of magnitude.** It listed AAPL at $190 (real, today:
$308.26) and, more to the point, was built around a ticker set that PLAN.md §7 replaced. MU alone
trades near $878 having touched $1,213. Hardcoded 2024 prices would make the workstation obviously
fake to anyone who knows the market — which is exactly the audience for a capstone demo.

---

## 3. Seed prices and parameters

`SEED_PRICES` is the **`SIMULATED`-mode fallback only**. In `ANCHORED` mode these are overridden by
real closes from `MassiveAnchorProvider`, so the table's staleness matters only when no key is set.

```python
# backend/app/market/seeds.py
# Calibrated from Massive daily bars, 2025-12-01 -> 2026-08-07 (pulled 2026-08-10).
# Refresh with scripts/calibrate_market.py (see MARKET_SIMULATOR.md §9).

SEED_PRICES: dict[str, float] = {
    "ALAB": 334.17, "MRVL": 218.72, "MU":   877.57, "AMD":  483.36, "INTC": 101.65,
    "PLTR": 172.01, "ANET": 188.67, "LRCX": 311.35, "AMAT": 539.14, "SLV":   57.50,
}

# sigma = realised annualised vol; mu = damped drift (see §6, NOT the realised value).
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "ALAB": {"sigma": 1.060, "mu": 0.16},
    "MRVL": {"sigma": 0.839, "mu": 0.16},
    "MU":   {"sigma": 0.885, "mu": 0.20},
    "AMD":  {"sigma": 0.720, "mu": 0.14},
    "INTC": {"sigma": 0.835, "mu": 0.17},
    "PLTR": {"sigma": 0.629, "mu": 0.02},
    "ANET": {"sigma": 0.573, "mu": 0.07},
    "LRCX": {"sigma": 0.692, "mu": 0.13},
    "AMAT": {"sigma": 0.644, "mu": 0.13},
    "SLV":  {"sigma": 0.749, "mu": 0.04},
}

DEFAULT_PARAMS = {"sigma": 0.45, "mu": 0.05}
```

A ticker with no entry — user-added or LLM-added — gets `DEFAULT_PARAMS` and, absent a real anchor,
a start price drawn from `Uniform(40, 400)`. `σ = 0.45` is deliberately between the calm-market norm
and this basket's extremes.

---

## 4. Sector correlation, measured

Real stocks co-move; independent random walks look wrong the moment two tickers are on screen. The
realised correlation matrix of daily log-returns over the calibration window:

```
         ALAB   MRVL     MU    AMD   INTC   PLTR   ANET   LRCX   AMAT    SLV
 ALAB    1.00   0.59   0.47   0.61   0.49   0.20   0.48   0.59   0.55   0.18
 MRVL    0.59   1.00   0.53   0.57   0.49   0.14   0.43   0.62   0.63   0.24
   MU    0.47   0.53   1.00   0.63   0.60   0.06   0.43   0.79   0.75   0.27
  AMD    0.61   0.57   0.63   1.00   0.66   0.19   0.46   0.70   0.67   0.30
 INTC    0.49   0.49   0.60   0.66   1.00   0.03   0.37   0.62   0.60   0.22
 PLTR    0.20   0.14   0.06   0.19   0.03   1.00   0.21   0.09   0.06   0.22
 ANET    0.48   0.43   0.43   0.46   0.37   0.21   1.00   0.49   0.51   0.21
 LRCX    0.59   0.62   0.79   0.70   0.62   0.09   0.49   1.00   0.92   0.32
 AMAT    0.55   0.63   0.75   0.67   0.60   0.06   0.51   0.92   1.00   0.28
  SLV    0.18   0.24   0.27   0.30   0.22   0.22   0.21   0.32   0.28   1.00
```

The structure is clean enough to encode as blocks:

- **LRCX ↔ AMAT = 0.92.** Both sell wafer-fab equipment into the same capex cycle; they are near
  duplicates. Any model treating them as loosely coupled tech names is wrong.
- **Memory/logic ↔ semi-cap ≈ 0.65–0.79.** MU's fortunes drive equipment orders.
- **The five semis cluster at ≈ 0.55.**
- **PLTR is nearly independent (0.03–0.21).** It is a software name in a semiconductor list.
- **SLV is nearly independent (0.18–0.32).** A commodity ETF, correlated only through macro risk.

Encoding these as blocks rather than a frozen 10×10 matrix is what lets the model extend to tickers
the user adds later:

```python
SECTORS: dict[str, str] = {
    "ALAB": "semi", "MRVL": "semi", "MU": "semi", "AMD": "semi", "INTC": "semi",
    "LRCX": "semicap", "AMAT": "semicap",
    "ANET": "networking",
    "PLTR": "software",
    "SLV": "commodity",
}

SECTOR_RHO: dict[tuple[str, str], float] = {
    ("semi", "semi"):        0.55,
    ("semicap", "semicap"):  0.90,
    ("semi", "semicap"):     0.65,
    ("networking", "semi"):  0.45,
    ("networking", "semicap"): 0.50,
    ("software", "*"):       0.15,   # PLTR: near-independent
    ("commodity", "*"):      0.25,   # SLV: macro only
}
DEFAULT_RHO = 0.35
```

---

## 5. The volatility budget — why the archived event model was broken

Jumps are not free decoration. A Bernoulli(`p`) jump of magnitude `Uniform(a, b)` adds annualised
variance:

```
Var_jump = p · E[J²] · ticks_per_year        where  E[J²] = (a² + ab + b²)/3
```

The archived draft specified `p = 0.001` with 2–5 % shocks against an intended `σ = 0.25`. Evaluating:

```
E[J²]    = (0.02² + 0.02·0.05 + 0.05²)/3 = 1.30e-03
Var_jump = 0.001 × 1.30e-03 × 11,793,600  = 15.33
σ_jump   = √15.33                          = 3.92
```

**392 % annualised volatility from the jump term alone — 16× the intended σ, and ~370× the variance
of the diffusion term it was layered onto.** The GBM parameters would have been almost irrelevant;
the tape would have been pure noise, and every calibrated `sigma` cosmetic. Its stated cadence
(~one event per ticker per 500 s) is also far too frequent to read as an "event".

### The fix: subtract the jump variance

Pick the jump parameters for *drama*, then solve the diffusion σ so the **total** matches the
calibrated target:

```
σ_d = √( max( σ_target² − Var_jump , ε ) )
```

With `p = 1e-4` and shocks of ±0.5–1.5 %:

```
E[J²]    = (0.005² + 0.005·0.015 + 0.015²)/3 = 1.083e-04
Var_jump = 1e-4 × 1.083e-04 × 11,793,600      = 0.1278      (σ_jump = 0.357)
events   = 1e-4 × 46,800 ticks/day            ≈ 4.7 per ticker per trading day
```

Monte-Carlo check (60 simulated trading days × 5 seeds, realised vol measured from daily closes):

| σ target | σ_d derived | σ realised | Error |
|---:|---:|---:|---:|
| 0.573 | 0.448 | 0.592 | +3.3 % |
| 0.749 | 0.658 | 0.769 | +2.7 % |
| 0.885 | 0.810 | 0.905 | +2.3 % |
| 1.060 | 0.998 | 1.080 | +1.9 % |

Realised volatility lands within ~3 % of target across the range. The small positive bias is
expected — `log(1+J) ≈ J − J²/2`, so the arithmetic-space variance formula slightly under-counts —
and 3 % is far inside the tolerance of a simulated demo. Jumps supply ~16 % of total variance:
enough to see, not enough to dominate.

Sanity-check on visibility at these price levels:

| Ticker | Price | 1σ per-tick move |
|---|---:|---|
| MU | $877.57 | $0.207 (2.4 bps) |
| INTC | $101.65 | $0.022 (2.2 bps) |
| SLV | $57.50 | $0.011 (1.9 bps) |

All comfortably above one cent, so the green/red flash fires on essentially every tick. Keep **4
decimal places** in the cache and round for display only — a hypothetical $3 stock would move
~$0.0006 per tick and would appear frozen if rounded to cents at the source.

---

## 6. Drift: damp it, deliberately

The realised μ column in §2 runs 0.24 → 2.30 annualised. **Do not use those values.**

They are the artefact of an eight-month AI-semiconductor melt-up. Feeding μ = 2.3 into the engine
would compound MU upward ~0.9 % per simulated day, every day, forever. Two consequences: the P&L
chart becomes a monotonic ramp, and every position the user or the LLM opens is profitable, which
makes the portfolio heatmap's red/green encoding meaningless.

Drift is also nearly invisible at demo timescales — it scales with `t` while volatility scales with
`√t`. Over one simulated hour at MU's numbers, drift contributes ~0.14 % against ~2.2 % of
volatility: about 6 % of the move. Removing 90 % of the drift costs almost nothing visually and buys
a two-sided market.

`TICKER_PARAMS` therefore uses **μ ≈ 10 % of realised**, capped at 0.20. That keeps the mild
sector tilt (semis drift up a little, PLTR and SLV barely at all) without making profit inevitable.
Setting every μ to 0.0 is also defensible and makes the simulation a martingale — a reasonable
choice for E2E test determinism.

---

## 7. Implementation

```python
# backend/app/market/simulator.py
import math
import random

SECONDS_PER_TRADING_YEAR = 252 * 6.5 * 3600      # 5,896,800
TICK_SECONDS = 0.5
DT = TICK_SECONDS / SECONDS_PER_TRADING_YEAR     # 8.4792e-08
TICKS_PER_YEAR = SECONDS_PER_TRADING_YEAR / TICK_SECONDS

JUMP_PROB = 1e-4
JUMP_MIN, JUMP_MAX = 0.005, 0.015
_JUMP_E_SQ = (JUMP_MIN**2 + JUMP_MIN * JUMP_MAX + JUMP_MAX**2) / 3.0
JUMP_VARIANCE = JUMP_PROB * _JUMP_E_SQ * TICKS_PER_YEAR      # 0.1278


def diffusion_sigma(target_sigma: float) -> float:
    """Diffusion vol such that diffusion + jumps realise `target_sigma`. See §5."""
    return math.sqrt(max(target_sigma**2 - JUMP_VARIANCE, 1e-6))


class GBMEngine:
    """Correlated jump-diffusion price paths. Synchronous, pure-stdlib, no I/O."""

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self._tickers: list[str] = []
        self._price: dict[str, float] = {}
        self._drift: dict[str, float] = {}      # precomputed (mu - sigma_d^2/2)*dt
        self._vol: dict[str, float] = {}        # precomputed sigma_d*sqrt(dt)
        self._chol: list[list[float]] | None = None

    # ---- membership -------------------------------------------------
    def add_ticker(self, ticker: str, start_price: float | None = None) -> None:
        if ticker in self._price:
            return
        params = TICKER_PARAMS.get(ticker, DEFAULT_PARAMS)
        sigma_d = diffusion_sigma(params["sigma"])
        self._tickers.append(ticker)
        self._price[ticker] = (
            start_price
            or SEED_PRICES.get(ticker)
            or self._rng.uniform(40.0, 400.0)
        )
        self._drift[ticker] = (params["mu"] - 0.5 * sigma_d**2) * DT
        self._vol[ticker] = sigma_d * math.sqrt(DT)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        if ticker not in self._price:
            return
        self._tickers.remove(ticker)
        for d in (self._price, self._drift, self._vol):
            d.pop(ticker, None)
        self._rebuild_cholesky()

    # ---- the step ---------------------------------------------------
    def step(self) -> dict[str, float]:
        """Advance one tick. Returns {ticker: price} at full precision."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z_ind = [self._rng.gauss(0.0, 1.0) for _ in range(n)]
        if self._chol is None:
            z = z_ind
        else:
            z = [sum(self._chol[i][k] * z_ind[k] for k in range(i + 1)) for i in range(n)]

        out: dict[str, float] = {}
        for i, t in enumerate(self._tickers):
            p = self._price[t] * math.exp(self._drift[t] + self._vol[t] * z[i])
            if self._rng.random() < JUMP_PROB:
                shock = self._rng.uniform(JUMP_MIN, JUMP_MAX)
                p *= 1.0 + (shock if self._rng.random() < 0.5 else -shock)
            self._price[t] = p
            out[t] = p
        return out

    def price(self, ticker: str) -> float | None:
        return self._price.get(ticker)

    # ---- correlation -------------------------------------------------
    def _rebuild_cholesky(self) -> None:
        n = len(self._tickers)
        if n <= 1:
            self._chol = None
            return
        m = [
            [1.0 if i == j else sector_rho(a, b) for j, b in enumerate(self._tickers)]
            for i, a in enumerate(self._tickers)
        ]
        self._chol = _cholesky(m) or _cholesky(_ridge(m, 0.05)) or None


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
    return [[m[i][j] * (1 - eps) + (eps if i == j else 0.0) for j in range(n)]
            for i in range(n)]
```

### Notes on the implementation

- **Pure stdlib — no numpy.** At n ≤ ~50 tickers the Cholesky is microseconds and the matrix is
  rebuilt only on membership change, not per tick. Skipping numpy keeps the Python image slim,
  which matters for PLAN.md §11's single-container build.
- **`_ridge` is defensive, and currently never fires.** Every configuration was checked: the seed
  watchlist, each sector block alone, 50 unknown tickers, and pathological cases such as 25 tickers
  all inside the ρ = 0.90 `semicap` block — all positive-definite, as is the realised empirical
  matrix. Equicorrelation blocks are PD whenever `−1/(n−1) < ρ < 1`; the risk comes from *editing*
  `SECTOR_RHO` into an inconsistent triangle later, which the ridge absorbs instead of crashing the
  price feed. Assert PD in a unit test so a bad edit fails in CI, not in the demo.
- **`z` uses only `k ≤ i`** because `L` is lower-triangular — the upper entries are structurally
  zero, so the loop is half the work of a dense multiply.
- **Drift and vol are precomputed per ticker** at `add_ticker`, so the hot loop is one `exp`, one
  Gaussian, and the triangular product.
- **`step()` returns full precision.** Rounding happens at the SSE boundary (§5).
- **`random.Random(seed)` is instance-local**, never the module-global RNG, so tests are
  deterministic and cannot be perturbed by other code drawing randomness.

---

## 8. Anchoring: real prices, simulated motion

In `ANCHORED` mode `MarketDataService` resolves anchors *before* priming the engine, so paths start
from genuine market levels:

```python
anchors = await MassiveAnchorProvider(client).anchors(["MU", "AMD", "SLV"])
# -> {"MU": 877.57, "AMD": 483.36, "SLV": 57.50}   (one grouped-daily call)

for ticker, anchor in anchors.items():
    cache.seed(ticker, anchor)                 # fixes open_price
    engine.add_ticker(ticker, start_price=anchor)
```

`cache.seed()` fixes `open_price` to the same real close the engine starts from, so the watchlist's
daily change % begins at exactly 0.00 % and drifts realistically — matching PLAN.md §6's requirement
that `open_price` be "set once when the ticker enters the cache and not overwritten by ticks."

This is the mode this repo actually runs in, and it is the most convincing of the three: a viewer
can cross-check MU against any finance site and find the level right, while the tape moves at 500 ms.

---

## 9. Recalibration

Seed prices and volatilities age. Ship the calibration as a script so refreshing is one command
rather than an archaeology exercise:

```
scripts/calibrate_market.py   ->   rewrites backend/app/market/seeds.py

**Written 2026-08-17.** It does what this section asked, plus a bars cache
(`backend/calibration/bars.json`) so adding one ticker costs one request rather than
refetching the set - correlations need aligned series, so the raw closes have to be kept,
not just the derived parameters. Both guards below are implemented.
```

It should pull `list_aggs(timespan="day")` for each seed ticker over ~8 months, compute annualised σ
from daily log-returns, damp μ per §6, and emit the module with a provenance header naming the date
range. **Throttle to ≤ 5 requests/minute** — one call per ticker on a free key exceeds the limit
otherwise (the original run slept 13 s between calls).

Two guards worth building in: reject a run where any ticker returns fewer than ~60 bars, and print a
before/after diff so a bad pull cannot silently overwrite good parameters.

Correlations move more slowly than prices; re-deriving `SECTOR_RHO` once or twice a year is enough.

---

## 10. File structure

```
backend/app/market/
├── simulator.py    # GBMEngine, diffusion_sigma, sector_rho, _cholesky, SimulatedSource
└── seeds.py        # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, SECTORS, SECTOR_RHO
```

`seeds.py` is data only — generated by §9's script, and the only file recalibration touches.
`simulator.py` holds the engine plus the thin `SimulatedSource` adapter from
[MARKET_INTERFACE.md §4](MARKET_INTERFACE.md#4-implementations).

---

## 11. Testing

Per PLAN.md §12 ("simulator generates valid prices, GBM math is correct"):

- **Positivity and finiteness** — 100,000 steps across the seed watchlist: every price `> 0`, no
  `inf`, no `nan`. Guaranteed by construction (`exp` is positive, `|J| ≤ 0.015`), so this is a
  regression guard against a future additive-noise "optimisation".
- **Volatility calibration** — simulate ≥ 60 trading days, measure annualised σ from daily closes,
  assert within ±15 % of target. The §5 table shows ~3 % in practice; the loose bound keeps the test
  from flaking on RNG variance. Run it seeded.
- **Volatility budget** — `diffusion_sigma(σ)² + JUMP_VARIANCE == σ²` for σ above the jump floor, and
  `diffusion_sigma` clamps rather than taking `√` of a negative when a target σ is below `σ_jump`
  (0.357). A low-volatility ticker is the edge case here.
- **Correlation recovery** — simulate two `semicap` tickers, correlate their log-returns, expect
  ≈ 0.90 ± 0.10. Repeat for a `semi`/`software` pair, expect ≈ 0.15.
- **Positive-definiteness** — assert `_cholesky` succeeds without the ridge for the seed watchlist,
  each sector block in isolation, and a 50-ticker unknown set. This is the test that catches a bad
  `SECTOR_RHO` edit.
- **Determinism** — two `GBMEngine(seed=42)` instances produce identical sequences. Required for the
  `LLM_MOCK=true` E2E runs in PLAN.md §12 to be reproducible.
- **Membership churn** — add and remove tickers mid-run; surviving tickers keep their prices, removed
  ones vanish from `step()`, and the Cholesky rebuild does not raise at n = 0 or n = 1.
- **Anchoring** — `add_ticker("MU", start_price=877.57)` starts exactly there, ignoring `SEED_PRICES`.

---

## Appendix: parameter summary

| Parameter | Value | Source |
|---|---|---|
| Tick interval | 500 ms | PLAN.md §6 |
| `dt` | 8.4792e-08 | 0.5 / (252 × 6.5 × 3600) |
| Ticks per trading year | 11,793,600 | — |
| `JUMP_PROB` | 1e-4 | ≈ 4.7 events/ticker/day (§5) |
| Jump magnitude | ±0.5 % – 1.5 % | §5 |
| `JUMP_VARIANCE` | 0.1278 | ≈ 16 % of total variance at σ = 0.885 |
| σ range (seeds) | 0.573 – 1.060 | Realised, Massive daily bars |
| μ | ≈ 10 % of realised, cap 0.20 | §6 |
| `DEFAULT_PARAMS` | σ = 0.45, μ = 0.05 | Unknown tickers |
| `DEFAULT_RHO` | 0.35 | §4 |
| Calibration window | 2025-12-01 → 2026-08-07 (172 bars) | Pulled 2026-08-10 |
