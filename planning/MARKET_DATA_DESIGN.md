# Market Data Backend — Detailed Design

The implementation guide for everything under `backend/app/market/`: the unified interface every
consumer codes against, the GBM simulator, and the Massive REST integration. It is written to be
executable — an agent should be able to build the package from this document alone, then use the
companions for the *reasoning* behind the numbers.

**Companions (read for derivations, not for interfaces):**

| Document | What it owns |
|---|---|
| [PLAN.md](PLAN.md) §3, §6, §8 | Product requirements: cache fields, SSE cadence, endpoint list, single-worker constraint |
| [MARKET_INTERFACE.md](MARKET_INTERFACE.md) | Why the three-mode design exists; the source/anchor split |
| [MARKET_SIMULATOR.md](MARKET_SIMULATOR.md) | GBM calibration, the volatility budget, correlation measurements |
| [MASSIVE_API.md](MASSIVE_API.md) | Endpoint shapes, entitlement probe results, rate limits — **all live-verified 2026-08-10** |
| [Review.md](Review.md) | The findings this document closes (§2 maps each one) |

**This document is authoritative for:** module boundaries, class signatures, the SSE wire format,
the HTTP payload shapes under `/api/prices/*` and `/api/stream/*`, and the Python API other backend
modules call. Where it disagrees with `planning/archive/`, this document wins — the archive predates
PLAN.md §13 and reintroduces bugs that review already removed.

---

## Table of Contents

1. [The shape of the problem](#1-the-shape-of-the-problem)
2. [Decisions this document closes](#2-decisions-this-document-closes)
3. [File layout and public surface](#3-file-layout-and-public-surface)
4. [Data model — `models.py`](#4-data-model--modelspy)
5. [Price cache — `cache.py`](#5-price-cache--cachepy)
6. [Interfaces — `source.py`](#6-interfaces--sourcepy)
7. [Simulator — `seeds.py` + `simulator.py`](#7-simulator--seedspy--simulatorpy)
8. [Massive integration — `massive.py`](#8-massive-integration--massivepy)
9. [The service — `service.py`](#9-the-service--servicepy)
10. [Factory and configuration — `__init__.py`](#10-factory-and-configuration--__init__py)
11. [HTTP surface — `routes.py`](#11-http-surface--routespy)
12. [Integration with the rest of the backend](#12-integration-with-the-rest-of-the-backend)
13. [Failure modes](#13-failure-modes)
14. [Testing](#14-testing)
15. [Build order](#15-build-order)
16. [Appendix A — worked example: a session on this repo's key](#appendix-a--worked-example-a-session-on-this-repos-key)
17. [Appendix B — configuration summary](#appendix-b--configuration-summary)

---

## 1. The shape of the problem

Three facts drive every decision below.

**1. This repo's Massive key cannot deliver live prices.** Probed live
([MASSIVE_API.md §9](MASSIVE_API.md#9-verification-log)): both snapshot endpoints return
`403 NOT_AUTHORIZED` on Basic. The newest datum obtainable is the previous session's close. So
PLAN.md §5's binary "key set → real data" is unachievable as written, and the resolution is three
modes:

| Mode | Trigger | Price motion | `open_price` anchor |
|---|---|---|---|
| `SIMULATED` | No key | GBM engine | Static seed table |
| `ANCHORED` | Key present, snapshots 403 (**this repo**) | GBM engine | **Real** previous close from Massive |
| `LIVE` | Key present, snapshots entitled (Starter+) | Massive polling | Real previous close |

The mode is **detected at startup, never configured**. A `MODE` env var would only let a deployment
lie about what its key can do.

**2. Poll cadence and stream cadence are different numbers.** The simulator produces a price every
500 ms; Massive on a free key allows 5 requests per *minute*. Both must look identical to the
frontend. The cache is what decouples them: one writer at `poll_interval`, one reader at the SSE
cadence, no consumer aware of which source is behind it.

**3. Everything shares one event loop.** PLAN.md §3 mandates a single uvicorn worker. The `massive`
SDK is synchronous urllib3 with `retries=3` and a 10 s read timeout, so one un-offloaded call can
stall every SSE connection in the process for ~30 seconds. **Every provider call goes through
`asyncio.to_thread`, without exception** ([Review.md C1](Review.md)).

---

## 2. Decisions this document closes

Each row is a question left open by PLAN.md or flagged in Review.md. The decision is binding on the
implementing agent.

| # | Question | Decision | Review ref |
|---|---|---|---|
| D1 | One SSE event per ticker, or one per frame? | **One event carrying every tracked ticker.** ~30 tickers is a small payload and the client gets a consistent snapshot. | A2 |
| D2 | Named SSE events or default `message`? | **Default `message` (bare `data:`)**, with a `type` field *inside* the JSON envelope. Client wires one `onmessage`, switches on `type`. Gets A2's simplicity without losing the `hello` metadata. | A2 |
| D3 | Is `retry:` sent? | **Yes, `retry: 1000`** as the first line of every stream. | A2 |
| D4 | Re-send unchanged prices every 500 ms? | **No.** The cache carries a monotonic `version`; the generator emits only when it advances, plus a `: ping` keepalive every 15 s. Under Massive this turns 29-of-30 dead frames into silence. | A3 |
| D5 | What is `prev_price`? | **The price at the previous cache *write*** — never per broadcast. This is what makes the green/red flash fire once per real tick under both simulator and Massive. | A3 |
| D6 | How do sparklines get seeded? | **`GET /api/prices/history?tickers=A,B,C&limit=60`** — one bulk call, evenly *subsampled* across the buffer (not the last 60 points, which would span 30 seconds and look flat). | A4 |
| D7 | Seed prices for the real watchlist | `seeds.py` from [MARKET_SIMULATOR.md §3](MARKET_SIMULATOR.md), calibrated against Massive daily bars. The archive's AAPL/GOOGL table is dead. | A5 |
| D8 | Ticker normalization | `normalize_ticker()` in `app/market/__init__.py`, applied at **every** boundary: REST, LLM output, cache, engine. `strip().upper()`, validated against `^[A-Z]{1,5}([.-][A-Z]{1,2})?$`. | B1 |
| D9 | Unknown/invalid ticker | In Massive modes, validated against the **grouped-daily universe already in memory** (~12,400 symbols, zero extra requests). Unknown → `400`. In `SIMULATED`, pattern check only. | B2 |
| D10 | A ticker added mid-session has no price for 15 s | `service.add_ticker()` resolves the anchor, seeds the cache, primes the source, and (in `LIVE`) polls that one ticker **before returning**. The route responds with a price already in hand. | B3 |
| D11 | `open_price` never rolls over | Session roll at **09:30 America/New_York**, checked once per loop iteration. Behaviour differs per mode — see [§9.3](#93-session-rollover). | B4 |
| D12 | What anchors daily change under Massive? | **The previous session's close**, matching every finance site and Massive's own `change_percent`. Not `day.open` (which is 0 pre-market → divide-by-zero). The field keeps the name `open_price` for PLAN.md §6 compatibility; its meaning is documented as "session anchor". | B5 |
| D13 | Deterministic simulator for E2E | `SIM_SEED` env var seeds an instance-local `random.Random`. Fixed `dt` per tick (not wall-clock-scaled) so a seeded run is bit-reproducible. | B10 |
| D14 | Timestamp convention | **Internally** float Unix seconds. **On the market-data wire** (`/api/stream/prices`, `/api/prices/*`) integer **epoch milliseconds** — charting libraries consume it directly and it is emitted twice a second. **Everywhere else** (DB columns, `/api/portfolio`, `/api/chat`) ISO 8601 UTC `Z` per Review B14. One convention per layer, converted only at the market adapter. | B14 |
| D15 | Valuation before the first tick | `cache.get()` returns `None` — it never invents a price. Callers fall back to `avg_cost`; the snapshot task skips writing until every position ticker has a quote. | B16 |
| D16 | Cache eviction | A ticker leaving the tracked set is **evicted**: quote, ring buffer, and anchor all dropped; the engine releases its state. Re-adding restarts daily change at 0.00 %. Acceptable and now stated. | D4 |
| D17 | Wire casing | **snake_case**, matching Python and the DB. The frontend maps once at its fetch layer. | A1 |
| D18 | Market closed in `LIVE` mode | Optional `HybridLiveSource` ([§8.5](#85-optional-closed-market-continuity)) keeps the tape moving with simulated motion anchored to the last real print. Health always reports `market_status` so a frozen tape is explained, never mysterious. | C2 |

---

## 3. File layout and public surface

```
backend/app/market/
├── __init__.py      # re-exports everything below — the only import surface
├── models.py        # Tick, Quote, Direction
├── cache.py         # PriceCache
├── source.py        # MarketDataSource (ABC), AnchorProvider (Protocol), Mode, Entitlement
├── symbols.py       # normalize_ticker, InvalidTicker  — imported by routes AND __init__
├── deps.py          # get_service  — FastAPI dependency, reads app.state
├── seeds.py         # SEED_PRICES, TICKER_PARAMS, SECTORS, SECTOR_RHO  — data only, generated
├── simulator.py     # GBMEngine, SimulatedSource, StaticAnchorProvider
├── massive.py       # MassiveGateway, MassiveAnchorProvider, MassiveLiveSource, probe_entitlement
├── service.py       # MarketDataService — the loop, the tracked set, the mode
└── routes.py        # APIRouter: /api/stream/prices, /api/prices/*  + health fragment
```

`symbols.py` and `deps.py` are one function each and exist purely to break a cycle: `__init__.py`
imports `routes` (to export the router), and `routes` needs `normalize_ticker` and `get_service`.
Putting those two in leaf modules that import nothing from the package keeps the import graph
acyclic. Both are re-exported from `__init__.py`, so callers still write
`from app.market import normalize_ticker`.

**Nothing outside `app/market/` imports anything but `app.market`.** No route, no LLM tool, no
portfolio function constructs a `RESTClient` or touches `PriceCache` directly — they receive the
service via dependency injection. This is the boundary that makes swapping the source a
one-line change in the factory.

The whole public surface, for reference while reading the rest:

```python
# backend/app/market/__init__.py  (surface only — full file in §10)

from .cache import PriceCache
from .deps import get_service
from .models import Direction, Quote, Tick
from .routes import router
from .service import MarketDataService
from .source import Mode
from .symbols import InvalidTicker, normalize_ticker

__all__ = [
    "PriceCache", "Quote", "Tick", "Direction", "Mode",
    "MarketDataService", "build_market_service",
    "normalize_ticker", "InvalidTicker", "get_service", "router",
]
```

Consumers use exactly six things:

```python
service.price("MU")               # float | None  — fill price, valuation
service.quote("MU")               # Quote | None  — full cache entry
await service.add_ticker("PYPL")  # Quote | None  — immediate, priced before it returns
await service.sync_tracked(watchlist | position_tickers)
await service.validate("ASDF")    # bool — symbol exists at the provider
service.mode, service.healthy     # Mode, bool — for /api/health and the SSE hello
```

---

## 4. Data model — `models.py`

Two types cross the boundary. No provider SDK object ever escapes an adapter.

```python
# backend/app/market/models.py
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal

Direction = Literal["up", "down", "flat"]


@dataclass(frozen=True, slots=True)
class Tick:
    """One price observation. What a source produces; the cache consumes it."""

    ticker: str
    price: float
    ts: float          # float Unix seconds — normalised at the adapter (D14)


@dataclass(slots=True)
class Quote:
    """Cached state for one ticker. Mirrors the PLAN.md §6 cache entry."""

    ticker: str
    price: float
    prev_price: float
    open_price: float                      # session anchor; written once per session (D12)
    ts: float
    session_date: str                      # ET session this anchor belongs to, "YYYY-MM-DD"
    history: deque[tuple[float, float]]    # bounded ring buffer of (ts, price)

    # ---- derived ----------------------------------------------------
    @property
    def change(self) -> float:
        """Tick delta — drives the flash, not the daily-change column."""
        return self.price - self.prev_price

    @property
    def direction(self) -> Direction:
        if self.price > self.prev_price:
            return "up"
        return "down" if self.price < self.prev_price else "flat"

    @property
    def day_change(self) -> float:
        return self.price - self.open_price

    @property
    def day_change_pct(self) -> float:
        if not self.open_price:            # guards pre-market 0.0 anchors (Review B5)
            return 0.0
        return (self.price - self.open_price) / self.open_price * 100.0

    # ---- wire -------------------------------------------------------
    def to_wire(self) -> dict[str, object]:
        """snake_case, epoch-ms, display-rounded. The only place rounding happens."""
        return {
            "ticker": self.ticker,
            "price": round(self.price, 4),
            "prev_price": round(self.prev_price, 4),
            "open_price": round(self.open_price, 4),
            "change": round(self.change, 4),
            "change_pct": round(self.day_change_pct, 3),
            "direction": self.direction,
            "ts": int(self.ts * 1000),
        }
```

Three invariants an implementer must not blur:

- **`change` and `day_change_pct` answer different questions.** `prev_price` is the last *tick*;
  differencing against it yields a per-tick delta near zero, not a daily move. Conflating them is
  the bug PLAN.md §13 item 3 exists to prevent.
- **Prices are kept at 4 decimals in the cache, rounded only in `to_wire()`.** At the seed
  watchlist's levels a 1σ tick is ~2 bps — $0.02 on INTC — but a hypothetical $3 stock moves
  ~$0.0006 per tick and would appear frozen if rounded to cents at the source
  ([MARKET_SIMULATOR.md §5](MARKET_SIMULATOR.md)).
- **`ts` is float seconds inside, epoch ms on the wire.** Massive mixes milliseconds (aggregate
  `t`) and nanoseconds (`updated`, `last_updated`); each adapter normalises at its own edge.

---

## 5. Price cache — `cache.py`

Single source of truth for "what is X worth right now". In-memory, bounded, restart-transient —
all three deliberate (PLAN.md §6).

```python
# backend/app/market/cache.py
from __future__ import annotations

import time
from collections import deque

from .models import Quote, Tick

HISTORY_MAXLEN = 1_000        # PLAN.md §6: ~8 min at 500ms, ~4h under a 15s Massive poll


class PriceCache:
    """In-memory quote store. Mutated only on the event-loop thread — no lock (see §5.2)."""

    def __init__(self, history_maxlen: int = HISTORY_MAXLEN) -> None:
        self._quotes: dict[str, Quote] = {}
        self._maxlen = history_maxlen
        self._version = 0

    # ---- change detection for SSE (D4) -------------------------------
    @property
    def version(self) -> int:
        """Monotonic counter, bumped by every mutation. The SSE generator gates on it."""
        return self._version

    # ---- writes ------------------------------------------------------
    def seed(self, ticker: str, anchor: float, *, ts: float | None = None,
             session_date: str = "") -> Quote:
        """Create the entry and fix its anchor. Idempotent — never re-anchors."""
        existing = self._quotes.get(ticker)
        if existing is not None:
            return existing
        ts = time.time() if ts is None else ts
        quote = Quote(
            ticker=ticker,
            price=anchor,
            prev_price=anchor,
            open_price=anchor,
            ts=ts,
            session_date=session_date,
            history=deque([(ts, anchor)], maxlen=self._maxlen),
        )
        self._quotes[ticker] = quote
        self._version += 1
        return quote

    def apply(self, tick: Tick) -> Quote:
        """Record a new price. Never touches open_price or session_date."""
        quote = self._quotes.get(tick.ticker)
        if quote is None:
            # Self-seeding is deliberate: a source that produces a ticker the service
            # has not seeded is a bug, but dropping the price would be a worse one.
            return self.seed(tick.ticker, tick.price, ts=tick.ts)
        quote.prev_price = quote.price
        quote.price = tick.price
        quote.ts = tick.ts
        quote.history.append((tick.ts, tick.price))
        self._version += 1
        return quote

    def reanchor(self, ticker: str, anchor: float, session_date: str,
                 *, rebase: bool = False) -> Quote | None:
        """Roll the session anchor (D11). `rebase` also moves the live price to `anchor`,
        which reads as an overnight gap — used in ANCHORED mode only."""
        quote = self._quotes.get(ticker)
        if quote is None:
            return None
        quote.open_price = anchor
        quote.session_date = session_date
        if rebase:
            quote.prev_price = quote.price
            quote.price = anchor
            quote.ts = time.time()
            quote.history.append((quote.ts, anchor))
        self._version += 1
        return quote

    def evict(self, ticker: str) -> None:
        """Drop a ticker leaving the tracked set — quote, buffer and anchor (D16)."""
        if self._quotes.pop(ticker, None) is not None:
            self._version += 1

    # ---- reads -------------------------------------------------------
    def get(self, ticker: str) -> Quote | None:
        """None means 'no price yet'. Never fabricate one — see D15."""
        return self._quotes.get(ticker)

    def price(self, ticker: str) -> float | None:
        quote = self._quotes.get(ticker)
        return quote.price if quote else None

    def snapshot(self) -> dict[str, Quote]:
        return dict(self._quotes)

    def tickers(self) -> set[str]:
        return set(self._quotes)

    def history(self, ticker: str, limit: int | None = None) -> list[tuple[float, float]]:
        """Recent (ts, price) points, evenly subsampled when `limit` is smaller than the
        buffer. Subsampling — not truncation — is what makes a 60-point sparkline span the
        whole window instead of the last 30 seconds (D6)."""
        quote = self._quotes.get(ticker)
        if quote is None:
            return []
        points = list(quote.history)
        if limit is None or limit <= 0 or len(points) <= limit:
            return points
        stride = len(points) / limit
        sampled = [points[int(i * stride)] for i in range(limit)]
        sampled[-1] = points[-1]           # always end on the live price
        return sampled
```

### 5.1 Why `seed()` and `apply()` are separate

`open_price` is written **once per session**, and no tick may overwrite it. That invariant is the
entire reason daily change % is computable (PLAN.md §13 item 3). Merging the two methods into an
"upsert" is the single most likely way for a future edit to break the watchlist's percentage column
silently — the number stays plausible, it just measures the wrong thing.

`reanchor()` is the only other writer of `open_price`, and it is called exactly once per ticker per
session by the service ([§9.3](#93-session-rollover)).

### 5.2 Concurrency

**All cache mutation happens on the event-loop thread.** Only the blocking HTTP call is offloaded
(`asyncio.to_thread` inside the Massive gateway); the returned ticks are applied back on the loop.
Under PLAN.md §3's single-worker model this makes the cache single-threaded, so it needs no lock.

The archived design wrapped every access in a `threading.Lock`. That is dead weight under this
invariant and — worse — advertises a thread-safety guarantee the rest of the system does not
maintain. **If a future change ever writes to the cache from a real thread, add the lock back and
update this paragraph.** Do not leave the two designs half-merged.

The *tracked set* is a different matter: mutating it involves `await`s, so it does need an
`asyncio.Lock` — see [§9.2](#92-the-tracked-set).

---

## 6. Interfaces — `source.py`

The archived design gave each source its own `start`/`stop`/polling loop, which duplicated the loop
per implementation and let the simulator and the poller drift apart. This design splits the concern:
**sources produce prices; one service owns the loop.**

```python
# backend/app/market/source.py
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Protocol

from .models import Tick


class Mode(StrEnum):
    SIMULATED = "simulated"
    ANCHORED = "anchored"
    LIVE = "live"


class Entitlement(StrEnum):
    SNAPSHOTS = "snapshots"     # Starter+  -> LIVE
    AGGREGATES = "aggregates"   # Basic     -> ANCHORED
    NONE = "none"               # bad key   -> SIMULATED


class AnchorProvider(Protocol):
    """Supplies the session anchor for tickers entering the tracked set, and answers
    'is this a real symbol?' — both are provider knowledge, so they live together."""

    async def anchors(self, tickers: list[str], session_date: str) -> dict[str, float]:
        """Return {ticker: anchor_price}. Omit tickers that cannot be resolved."""

    async def is_known(self, ticker: str) -> bool:
        """True if the provider recognises the symbol (D9)."""

    async def refresh(self, session_date: str) -> None:
        """Invalidate any per-session state. Called at the session roll."""


class MarketDataSource(ABC):
    """Produces prices for a set of tickers. Owns no loop, no cache, no tracked set."""

    #: Seconds between service calls to `poll()`.
    poll_interval: float = 0.5

    @abstractmethod
    async def prime(self, tickers: list[str], anchors: dict[str, float]) -> None:
        """Register tickers and their anchors before the first poll."""

    @abstractmethod
    async def poll(self, tickers: list[str]) -> list[Tick]:
        """Return the current price for each ticker. Called every `poll_interval`."""

    @abstractmethod
    async def release(self, ticker: str) -> None:
        """Drop per-ticker state. Called when a ticker leaves the tracked set."""

    async def rebase(self, ticker: str, price: float) -> None:
        """Move a ticker's price to `price` at a session roll. Default: no-op (a live
        source has nothing to rebase — the API is authoritative)."""

    async def aclose(self) -> None:
        """Release process-wide resources. Default: nothing."""
```

`poll()` is total and stateless from the caller's view: hand it tickers, get ticks back. No
implementation spawns a task, sleeps, or writes to the cache. That is what makes the conformance
test in [§14.1](#141-conformance-both-sources-one-interface) possible — the same suite runs against
both sources with no special-casing.

---

## 7. Simulator — `seeds.py` + `simulator.py`

Model, calibration, and the volatility-budget derivation are in
[MARKET_SIMULATOR.md](MARKET_SIMULATOR.md). Summary of what matters here: prices follow geometric
Brownian motion with a Poisson jump overlay; **jumps contribute variance, so they are budgeted
against the target volatility rather than added on top**; the seed basket is an AI-semiconductor
list with realised σ of 0.57–1.06, roughly 3× a typical large-cap.

### 7.1 `seeds.py` — data only

```python
# backend/app/market/seeds.py
# Calibrated from Massive daily bars, 2025-12-01 -> 2026-08-07 (pulled 2026-08-10).
# Regenerate with scripts/calibrate_market.py — see MARKET_SIMULATOR.md §9.
# This file is DATA. No logic, no imports from the rest of the package.

SEED_PRICES: dict[str, float] = {
    "ALAB": 334.17, "MRVL": 218.72, "MU": 877.57, "AMD": 483.36, "INTC": 101.65,
    "PLTR": 172.01, "ANET": 188.67, "LRCX": 311.35, "AMAT": 539.14, "SLV": 57.50,
}

# sigma = realised annualised vol; mu = DAMPED drift (~10% of realised, cap 0.20).
# Using realised mu (0.24–2.30) would make every position profitable and the
# heatmap's red/green encoding meaningless — MARKET_SIMULATOR.md §6.
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
FALLBACK_PRICE_RANGE = (40.0, 400.0)      # unknown ticker, no real anchor available

SECTORS: dict[str, str] = {
    "ALAB": "semi", "MRVL": "semi", "MU": "semi", "AMD": "semi", "INTC": "semi",
    "LRCX": "semicap", "AMAT": "semicap",
    "ANET": "networking",
    "PLTR": "software",
    "SLV": "commodity",
}

# Measured correlations, encoded as blocks so user-added tickers inherit sane values.
SECTOR_RHO: dict[tuple[str, str], float] = {
    ("semi", "semi"):          0.55,
    ("semicap", "semicap"):    0.90,   # LRCX/AMAT realised 0.92 — near duplicates
    ("semi", "semicap"):       0.65,
    ("networking", "semi"):    0.45,
    ("networking", "semicap"): 0.50,
    ("software", "*"):         0.15,   # PLTR: near-independent of this basket
    ("commodity", "*"):        0.25,   # SLV: macro only
}
DEFAULT_RHO = 0.35
```

### 7.2 `simulator.py`

```python
# backend/app/market/simulator.py
from __future__ import annotations

import math
import random
import time

from .models import Tick
from .seeds import (DEFAULT_PARAMS, DEFAULT_RHO, FALLBACK_PRICE_RANGE, SECTOR_RHO,
                    SECTORS, SEED_PRICES, TICKER_PARAMS)
from .source import MarketDataSource

SECONDS_PER_TRADING_YEAR = 252 * 6.5 * 3600          # 5,896,800
TICK_SECONDS = 0.5

JUMP_PROB = 1e-4                                     # ≈ 4.7 events/ticker/trading day
JUMP_MIN, JUMP_MAX = 0.005, 0.015
_JUMP_E_SQ = (JUMP_MIN**2 + JUMP_MIN * JUMP_MAX + JUMP_MAX**2) / 3.0


def diffusion_sigma(target_sigma: float, jump_variance: float) -> float:
    """Diffusion vol such that diffusion + jumps realise `target_sigma`.

    The archived design added jumps ON TOP of the target, producing 392% annualised
    vol from the jump term alone — 16x the intent. Subtracting is the fix.
    MARKET_SIMULATOR.md §5 has the arithmetic and the Monte-Carlo check (±3%).
    """
    return math.sqrt(max(target_sigma**2 - jump_variance, 1e-6))


class GBMEngine:
    """Correlated jump-diffusion price paths. Synchronous, pure stdlib, no I/O."""

    def __init__(self, seed: int | None = None, tick_seconds: float = TICK_SECONDS) -> None:
        self._rng = random.Random(seed)              # instance-local: tests cannot be
                                                     # perturbed by other code's draws
        self._dt = tick_seconds / SECONDS_PER_TRADING_YEAR
        self._sqrt_dt = math.sqrt(self._dt)
        self._jump_variance = JUMP_PROB * _JUMP_E_SQ * (SECONDS_PER_TRADING_YEAR / tick_seconds)

        self._tickers: list[str] = []
        self._price: dict[str, float] = {}
        self._drift: dict[str, float] = {}           # (mu - sigma_d^2/2) * dt, precomputed
        self._vol: dict[str, float] = {}             # sigma_d * sqrt(dt), precomputed
        self._chol: list[list[float]] | None = None

    # ---- membership --------------------------------------------------
    def add_ticker(self, ticker: str, start_price: float | None = None) -> None:
        if ticker in self._price:
            return
        params = TICKER_PARAMS.get(ticker, DEFAULT_PARAMS)
        sigma_d = diffusion_sigma(params["sigma"], self._jump_variance)
        self._tickers.append(ticker)
        self._price[ticker] = (
            start_price
            or SEED_PRICES.get(ticker)
            or self._rng.uniform(*FALLBACK_PRICE_RANGE)
        )
        self._drift[ticker] = (params["mu"] - 0.5 * sigma_d**2) * self._dt
        self._vol[ticker] = sigma_d * self._sqrt_dt
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        if ticker not in self._price:
            return
        self._tickers.remove(ticker)
        for store in (self._price, self._drift, self._vol):
            store.pop(ticker, None)
        self._rebuild_cholesky()

    def set_price(self, ticker: str, price: float) -> None:
        """Jump a path to a new level — used at the ANCHORED session roll (§9.3)."""
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
        matrix = [
            [1.0 if i == j else sector_rho(a, b) for j, b in enumerate(self._tickers)]
            for i, a in enumerate(self._tickers)
        ]
        self._chol = _cholesky(matrix) or _cholesky(_ridge(matrix, 0.05))


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

### 7.3 The source adapter and the static anchors

```python
class SimulatedSource(MarketDataSource):
    """Drives the GBM engine. Used by BOTH SimulatedSource and ANCHORED modes —
    the only difference between those two modes is which AnchorProvider fills
    `anchors`, which is why ANCHORED costs almost no extra code."""

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

    async def anchors(self, tickers: list[str], session_date: str) -> dict[str, float]:
        return {
            t: SEED_PRICES.get(t) or self._rng.uniform(*FALLBACK_PRICE_RANGE)
            for t in tickers
        }

    async def is_known(self, ticker: str) -> bool:
        return True          # no universe to check against; the regex is the only gate

    async def refresh(self, session_date: str) -> None:
        return None
```

**Implementation notes**

- **Pure stdlib — no numpy.** At n ≤ ~50 tickers the Cholesky is microseconds and it is rebuilt only
  on membership change, not per tick. Skipping numpy keeps the single-container image slim.
- **`_ridge` is defensive and currently never fires.** Every configuration was checked
  positive-definite ([MARKET_SIMULATOR.md §7](MARKET_SIMULATOR.md)); the risk it absorbs is a future
  inconsistent edit to `SECTOR_RHO`. Assert PD in a unit test so a bad edit fails in CI, not in the
  demo.
- **`dt` is fixed per tick, not scaled by wall-clock elapsed time.** A seeded run is therefore
  bit-reproducible (D13), at the cost of the simulated year advancing slightly slower than real time
  when the loop drifts. For a demo that trade is obviously correct.
- **`step()` returns full precision.** Rounding happens once, in `Quote.to_wire()`.

---

## 8. Massive integration — `massive.py`

Everything in this section is built on live-verified behaviour
([MASSIVE_API.md §4, §9](MASSIVE_API.md#4-entitlements--what-your-key-can-actually-call)). Two facts
dominate the design: **Basic allows 5 requests/minute**, and **both snapshot endpoints 403 on
Basic**. So the only viable free-tier price source is the one endpoint that returns many tickers per
call — the grouped daily aggregate.

### 8.1 The gateway: rate limiting + thread offload

Every provider call goes through one object. That object is the only place in the codebase that
knows the client is synchronous.

```python
# backend/app/market/massive.py
from __future__ import annotations

import asyncio
import functools
import logging
import time
from datetime import date, timedelta
from typing import Any, Callable, Iterable

from .models import Tick
from .seeds import FALLBACK_PRICE_RANGE, SEED_PRICES
from .source import Entitlement, MarketDataSource

log = logging.getLogger(__name__)

FREE_TIER_RPM = 5
PAID_TIER_RPM = 300


class RateLimiter:
    """Token bucket. Best-effort budget guard, not a hard contract — an auto-paginating
    generator spends more than one request per acquire (see `call_list`)."""

    def __init__(self, rate: int, per: float = 60.0) -> None:
        self._capacity = float(rate)
        self._tokens = float(rate)
        self._per = per
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:                     # holding across sleep serialises callers
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity,
                    self._tokens + (now - self._updated) * self._capacity / self._per,
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) * self._per / self._capacity)


class MassiveGateway:
    """Owns the RESTClient, the rate limiter, and the to_thread boundary.

    The `massive` SDK is synchronous urllib3 with retries=3 and a 10s read timeout, so a
    single un-offloaded call can stall the event loop — and therefore every SSE connection
    in the process — for ~30 seconds (Review.md C1). Nothing else in the package may call
    the client directly.
    """

    def __init__(self, api_key: str, rpm: int = FREE_TIER_RPM) -> None:
        from massive import RESTClient          # imported here so the package is optional

        self._client = RESTClient(api_key=api_key)   # explicit beats the env-var default
        self._limiter = RateLimiter(rpm)

    @property
    def client(self) -> Any:
        return self._client

    def set_rpm(self, rpm: int) -> None:
        self._limiter = RateLimiter(rpm)

    async def call(self, fn: Callable[..., Any], /, **kwargs: Any) -> Any:
        await self._limiter.acquire()
        return await asyncio.to_thread(functools.partial(fn, **kwargs))

    async def call_list(self, fn: Callable[..., Iterable[Any]], /, **kwargs: Any) -> list[Any]:
        """For generator endpoints (`list_aggs`, `list_universal_snapshots`). The generator
        MUST be drained inside the thread — returning it to the loop would move the blocking
        HTTP calls back onto the event loop, one per page."""
        await self._limiter.acquire()
        return await asyncio.to_thread(lambda: list(fn(**kwargs)))
```

### 8.2 Entitlement probe

One cheap call decides the mode for the whole process. A 403 is **permanent** for the life of the
key — probe once at startup, never retry it in a loop.

```python
async def probe_entitlement(gateway: MassiveGateway) -> Entitlement:
    """One request. Never raises — a bad market-data key must not crash startup."""
    try:
        await gateway.call(
            gateway.client.get_snapshot_all, market_type="stocks", tickers=["AAPL"]
        )
        return Entitlement.SNAPSHOTS
    except Exception as exc:                       # noqa: BLE001 — SDK raises broadly
        text = str(exc)
        if "NOT_AUTHORIZED" in text or "403" in text:
            return Entitlement.AGGREGATES
        if "401" in text or "UNAUTHORIZED" in text:
            return Entitlement.NONE
        log.warning("Massive entitlement probe inconclusive: %s", exc)
        return Entitlement.AGGREGATES              # aggregates is the safer assumption
```

This asymmetry with `OPENROUTER_API_KEY` is deliberate: chat is unusable without its key, so PLAN.md
§5 fails fast; market data always has a working fallback, so it degrades instead.

### 8.3 Anchors and symbol validation

One grouped-daily call resolves *every* ticker at once and — because it returns all ~12,400 US
symbols — doubles as the symbol-validation universe at **zero extra request cost**. That is what
makes both anchoring and ticker validation affordable on a 5-req/min key.

```python
GROUPED_LOOKBACK_DAYS = 7


class MassiveAnchorProvider:
    """Session anchors from the last completed trading session's close.

    Previous close — not the session open — because that is what every finance site and
    Massive's own change_percent use, and because day.open is 0 pre-market, which would
    divide by zero (Review.md B5 / D12).
    """

    def __init__(self, gateway: MassiveGateway) -> None:
        self._gw = gateway
        self._universe: dict[str, float] = {}      # ticker -> close, whole US market
        self._universe_session: str = ""
        self._lock = asyncio.Lock()

    # ---- AnchorProvider ---------------------------------------------
    async def anchors(self, tickers: list[str], session_date: str) -> dict[str, float]:
        universe = await self._ensure_universe(session_date)
        resolved: dict[str, float] = {}
        missing: list[str] = []
        for ticker in tickers:
            close = universe.get(ticker)
            if close:
                resolved[ticker] = close
            else:
                missing.append(ticker)

        for ticker in missing:                     # rare: IPO'd today, or a bad symbol
            close = await self._previous_close(ticker)
            if close:
                resolved[ticker] = close
            elif ticker in SEED_PRICES:
                log.warning("Massive has no bar for %s; using the seed table", ticker)
                resolved[ticker] = SEED_PRICES[ticker]
            else:
                log.warning("no anchor for %s; it will be skipped", ticker)
        return resolved

    async def is_known(self, ticker: str) -> bool:
        universe = await self._ensure_universe(self._universe_session)
        return ticker in universe

    async def refresh(self, session_date: str) -> None:
        async with self._lock:
            self._universe = {}
            self._universe_session = ""
        await self._ensure_universe(session_date)

    # ---- internals ---------------------------------------------------
    async def _ensure_universe(self, session_date: str) -> dict[str, float]:
        async with self._lock:
            if self._universe and self._universe_session == session_date:
                return self._universe
            self._universe = await self._load_universe()
            self._universe_session = session_date
            return self._universe

    async def _load_universe(self) -> dict[str, float]:
        """Walk back from YESTERDAY until a grouped call returns rows.

        Starting at yesterday rather than today is deliberate and matters on both tiers:
        on Basic today always returns empty (end-of-day data only), and on Starter+ today
        returns an IN-PROGRESS bar, which would make 'previous close' mean 'right now' and
        pin daily change at ~0%. The most recent completed session before today is the
        correct denominator during the session and after the close alike.
        """
        day = date.today() - timedelta(days=1)
        for _ in range(GROUPED_LOOKBACK_DAYS):
            bars = await self._gw.call(
                self._gw.client.get_grouped_daily_aggs,
                date=day.isoformat(),
                adjusted=True,
            )
            if bars:
                log.info("Massive grouped session %s: %d symbols", day, len(bars))
                return {b.ticker: b.close for b in bars if b.ticker and b.close}
            day -= timedelta(days=1)               # weekend / holiday
        log.error("no Massive session data in the last %d days", GROUPED_LOOKBACK_DAYS)
        return {}

    async def _previous_close(self, ticker: str) -> float | None:
        try:
            # NOTE: the SDK annotates this as a single object, but BaseClient maps the
            # deserializer over the results array — at runtime it is a LIST.
            # (MASSIVE_API.md §5.2.)
            aggs = await self._gw.call(
                self._gw.client.get_previous_close_agg, ticker=ticker, adjusted=True
            )
            for agg in aggs or []:
                if agg.close:
                    return float(agg.close)
        except Exception as exc:                   # noqa: BLE001
            log.warning("previous-close lookup failed for %s: %s", ticker, exc)
        return None
```

**Request budget on a Basic key.** The design has to fit inside 5 requests/minute:

| Event | Requests | Frequency |
|---|---|---|
| Entitlement probe | 1 | Once per process |
| Universe / anchors load | 1–7 (walkback) | Once per process, once per session roll |
| Ticker added mid-session, in universe | **0** | Served from the cached universe |
| Ticker added mid-session, not in universe | 1 | Rare (IPO, bad symbol) |
| Steady-state price polling | **0** | The GBM engine supplies motion |

In `ANCHORED` mode — the mode this repo actually runs in — steady state costs **zero requests**.
That is the whole point: a free key buys real *price levels*; the simulator supplies the *motion*
the free tier cannot.

### 8.4 `MassiveLiveSource` — Starter+ only

```python
SNAPSHOT_CHUNK = 250                              # v3 ticker.any_of hard limit


class MassiveLiveSource(MarketDataSource):
    """Polls real prices. Only reachable when the entitlement probe returns SNAPSHOTS."""

    def __init__(self, gateway: MassiveGateway, poll_interval: float = 15.0) -> None:
        self._gw = gateway
        self.poll_interval = poll_interval

    async def prime(self, tickers: list[str], anchors: dict[str, float]) -> None:
        return None                                # the API is authoritative

    async def poll(self, tickers: list[str]) -> list[Tick]:
        ticks: list[Tick] = []
        for i in range(0, len(tickers), SNAPSHOT_CHUNK):
            chunk = tickers[i:i + SNAPSHOT_CHUNK]
            snapshots = await self._gw.call_list(
                self._gw.client.list_universal_snapshots,
                type="stocks",
                ticker_any_of=chunk,
                limit=SNAPSHOT_CHUNK,              # default is 10 — silently truncates
            )
            for snap in snapshots:
                tick = self._to_tick(snap)
                if tick:
                    ticks.append(tick)
        return ticks

    async def release(self, ticker: str) -> None:
        return None                                # stateless

    @staticmethod
    def _to_tick(snap: Any) -> Tick | None:
        # A bad ticker fails INSIDE the results array, not as an HTTP error.
        if getattr(snap, "error", None):
            log.warning("snapshot error for %s: %s", snap.ticker, getattr(snap, "message", ""))
            return None
        session = getattr(snap, "session", None)
        price = getattr(session, "price", None) or getattr(session, "close", None)
        if not price:
            return None
        raw_ts = getattr(session, "last_updated", None)
        ts = raw_ts / 1e9 if raw_ts else time.time()      # v3 timestamps are NANOseconds
        return Tick(snap.ticker, float(price), ts)
```

`poll_interval` defaults to 15 s — the free-tier-safe cadence PLAN.md §6 names — and can drop to 2 s
on Advanced. **The poll interval and the SSE cadence are independent**: SSE emits from the cache
whenever it changes, so a 15 s poll produces a stepped chart, not a stalled one.

### 8.5 Optional: closed-market continuity

Outside 09:30–16:00 ET a `LIVE` source returns the same frozen price forever: no flashing, flat
sparklines, a motionless heatmap. For a developer in UTC-3, evening and weekend demos land squarely
in that dead zone ([Review.md C2](Review.md)). This wrapper keeps the tape alive without lying about
where the level came from.

```python
class HybridLiveSource(MarketDataSource):
    """Massive while the market is open; GBM motion anchored to the last real print when
    it is closed. Optional — skip it and the app still works, it just looks frozen at night."""

    STATUS_TTL = 60.0

    def __init__(self, live: MassiveLiveSource, sim: "SimulatedSource",
                 gateway: MassiveGateway) -> None:
        self._live, self._sim, self._gw = live, sim, gateway
        self.poll_interval = live.poll_interval
        self._status = "unknown"
        self._checked = 0.0

    async def prime(self, tickers: list[str], anchors: dict[str, float]) -> None:
        await self._live.prime(tickers, anchors)
        await self._sim.prime(tickers, anchors)

    async def poll(self, tickers: list[str]) -> list[Tick]:
        if await self._market_open():
            ticks = await self._live.poll(tickers)
            for tick in ticks:                    # keep the engine at the real level
                await self._sim.rebase(tick.ticker, tick.price)
            return ticks
        return await self._sim.poll(tickers)

    async def release(self, ticker: str) -> None:
        await self._live.release(ticker)
        await self._sim.release(ticker)

    async def rebase(self, ticker: str, price: float) -> None:
        await self._sim.rebase(ticker, price)

    @property
    def market_status(self) -> str:
        return self._status

    async def _market_open(self) -> bool:
        now = time.monotonic()
        if now - self._checked > self.STATUS_TTL:
            try:                                   # free on every plan, real-time
                status = await self._gw.call(self._gw.client.get_market_status)
                self._status = status.market or "unknown"
            except Exception as exc:               # noqa: BLE001
                log.warning("market status check failed: %s", exc)
            self._checked = now
        return self._status == "open"
```

Note the asymmetry in cadence: `poll_interval` stays at the live value (15 s) even while simulating,
so the closed-market tape is stepped rather than smooth. Setting `self.poll_interval = 0.5` while
closed is a legitimate refinement; it just means the service's sleep changes mid-run, which is
harmless because `_backoff()` re-reads it every iteration.

---

## 9. The service — `service.py`

One class owns the loop, the tracked set, the cache, the mode, and the session clock. It is the only
market object the FastAPI app talks to.

```python
# backend/app/market/service.py
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from .cache import PriceCache
from .models import Quote
from .source import AnchorProvider, MarketDataSource, Mode

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")       # requires `tzdata` on the dependency list
SESSION_OPEN = dtime(9, 30)
MAX_BACKOFF = 60.0
UNHEALTHY_AFTER = 3


def current_session_date(now: datetime | None = None) -> str:
    """FinAlly's session label: the ET calendar date, rolling at 09:30 ET.

    At 02:00 ET Tuesday this returns Monday — still inside Monday's session window as far
    as 'daily change' is concerned. Weekends produce a non-trading label, which is
    harmless: the anchor fetch walks back to the last day with real data anyway.
    """
    now_et = (now or datetime.now(tz=timezone.utc)).astimezone(ET)
    if now_et.time() < SESSION_OPEN:
        now_et -= timedelta(days=1)
    return now_et.date().isoformat()


class MarketDataService:
    def __init__(
        self,
        source: MarketDataSource,
        anchors: AnchorProvider,
        cache: PriceCache,
        mode: Mode,
    ) -> None:
        self._source = source
        self._anchors = anchors
        self._cache = cache
        self.mode = mode

        self._tracked: set[str] = set()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()          # guards the tracked set, not the cache (§5.2)
        self._session_date = current_session_date()
        self._last_ok = 0.0
        self._failures = 0

    # ---- lifecycle ----------------------------------------------------
    async def start(self, tickers: set[str]) -> None:
        await self._track(tickers)
        self._task = asyncio.create_task(self._run(), name="market-data")
        log.info(
            "market data: mode=%s source=%s tracking=%d session=%s",
            self.mode, type(self._source).__name__, len(self._tracked), self._session_date,
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._source.aclose()

    # ---- reads (the API other modules use) ----------------------------
    def quote(self, ticker: str) -> Quote | None:
        return self._cache.get(ticker)

    def price(self, ticker: str) -> float | None:
        """None means 'no price yet' — callers fall back to avg_cost (D15)."""
        return self._cache.price(ticker)

    @property
    def cache(self) -> PriceCache:
        return self._cache

    @property
    def tracked(self) -> set[str]:
        return set(self._tracked)

    @property
    def session_date(self) -> str:
        return self._session_date

    @property
    def poll_interval(self) -> float:
        """Surfaced in the SSE hello frame so the client knows whether to expect a smooth
        tape (0.5s) or a stepped one (15s under LIVE)."""
        return self._source.poll_interval

    @property
    def healthy(self) -> bool:
        return self._failures < UNHEALTHY_AFTER

    async def validate(self, ticker: str) -> bool:
        return await self._anchors.is_known(ticker)
```

### 9.1 Immediate add — the mechanism behind PLAN.md §9's auto-add

PLAN.md §9 asserts that trading an unwatched ticker "pulls it into the tracked set and gives it a
live price". Under a 15 s poll that is not automatic — the next poll may be 15 seconds away and the
trade needs a fill price *now*. `add_ticker` makes the assertion true by resolving everything before
it returns.

```python
    async def add_ticker(self, ticker: str) -> Quote | None:
        """Track one ticker and return it PRICED. Used by watchlist-add and by the LLM's
        auto-add path (PLAN.md §9). Resolves the anchor, seeds the cache, primes the
        source, and — in LIVE mode — polls that single ticker, all before returning."""
        async with self._lock:
            if ticker in self._tracked:
                return self._cache.get(ticker)

            resolved = await self._anchors.anchors([ticker], self._session_date)
            anchor = resolved.get(ticker)
            if anchor is None:
                log.warning("cannot anchor %s; not tracking it", ticker)
                return None

            quote = self._cache.seed(ticker, anchor, session_date=self._session_date)
            self._tracked.add(ticker)
            await self._source.prime([ticker], resolved)

        # One immediate poll so LIVE mode does not hand back a stale anchor as the fill
        # price. In SIMULATED/ANCHORED the anchor IS the price, so this is a cheap no-op
        # beyond a single GBM step.
        with contextlib.suppress(Exception):
            for tick in await self._source.poll([ticker]):
                if tick.ticker == ticker:
                    quote = self._cache.apply(tick)
        return quote
```

### 9.2 The tracked set

```python
    async def sync_tracked(self, tickers: set[str]) -> None:
        """Recompute the tracked set = watchlist ∪ open positions.
        Call after ANY watchlist change and after EVERY trade."""
        async with self._lock:
            added = tickers - self._tracked
            removed = self._tracked - tickers
            for ticker in removed:
                self._tracked.discard(ticker)
                await self._source.release(ticker)
                self._cache.evict(ticker)          # D16: buffer and anchor go too
        if added:
            await self._track(added)

    async def _track(self, tickers: set[str]) -> None:
        new = sorted(tickers - self._tracked)
        if not new:
            return
        resolved = await self._anchors.anchors(new, self._session_date)
        primed: list[str] = []
        for ticker in new:
            anchor = resolved.get(ticker)
            if anchor is None:
                log.warning("no anchor for %s; skipping", ticker)
                continue
            self._cache.seed(ticker, anchor, session_date=self._session_date)
            self._tracked.add(ticker)
            primed.append(ticker)
        if primed:
            await self._source.prime(primed, resolved)
```

**The tracked set is the union of the watchlist and open positions** — never the watchlist alone
(PLAN.md §6, §13 item 4). A user can buy a ticker and then remove it from the watchlist; the
position survives, and if its price stopped updating, portfolio value, the heatmap, and the P&L
chart would all silently go stale. This is worth an explicit regression test
([§14.4](#144-the-tracked-set-regression)).

The `asyncio.Lock` is not paranoia. `_track` awaits a network call in the middle of a
read-modify-write on `_tracked`; two concurrent watchlist adds without the lock will both see the
ticker as absent, both fetch an anchor, and both prime the engine. (The cache itself still needs no
lock — see [§5.2](#52-concurrency).)

### 9.3 Session rollover

`open_price` frozen forever turns "daily change" into "change since the container started". Two
tickers added on different days then display percentages measured against different anchors, side by
side, labelled identically ([Review.md B4](Review.md)).

```python
    async def _maybe_roll_session(self) -> None:
        today = current_session_date()
        if today == self._session_date:
            return
        log.info("session roll: %s -> %s (mode=%s)", self._session_date, today, self.mode)
        async with self._lock:
            self._session_date = today
            await self._anchors.refresh(today)

            if self.mode is Mode.SIMULATED:
                # No real data to re-anchor against: pin the anchor to where the path is
                # now, so daily change restarts at 0.00% with no price discontinuity.
                for ticker in sorted(self._tracked):
                    quote = self._cache.get(ticker)
                    if quote:
                        self._cache.reanchor(ticker, quote.price, today)
                return

            fresh = await self._anchors.anchors(sorted(self._tracked), today)
            for ticker, anchor in fresh.items():
                # ANCHORED re-bases the simulated path onto the new real close; the
                # resulting discontinuity IS an overnight gap, which is realistic.
                # LIVE only moves the anchor — the API keeps supplying the price.
                rebase = self.mode is Mode.ANCHORED
                self._cache.reanchor(ticker, anchor, today, rebase=rebase)
                if rebase:
                    await self._source.rebase(ticker, anchor)
```

### 9.4 The one polling loop

```python
    async def _run(self) -> None:
        while True:
            try:
                await self._maybe_roll_session()
                if self._tracked:
                    ticks = await self._source.poll(sorted(self._tracked))
                    for tick in ticks:
                        if tick.ticker in self._tracked:      # guard against a concurrent evict
                            self._cache.apply(tick)
                    self._last_ok = time.time()
                    self._failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:                                 # noqa: BLE001
                self._failures += 1
                log.exception("market poll failed (%d consecutive)", self._failures)
            await asyncio.sleep(self._backoff())

    def _backoff(self) -> float:
        base = self._source.poll_interval
        if not self._failures:
            return base
        return min(base * 2 ** min(self._failures, 5), MAX_BACKOFF)

    def health(self) -> dict[str, object]:
        """Fragment merged into GET /api/health."""
        return {
            "mode": str(self.mode),
            "source": type(self._source).__name__,
            "healthy": self.healthy,
            "tracked": len(self._tracked),
            "session_date": self._session_date,
            "last_tick_age_s": round(time.time() - self._last_ok, 2) if self._last_ok else None,
            "market_status": getattr(self._source, "market_status", None),
        }
```

Three properties of this loop matter more than they look:

- **A failed poll never clears the cache.** The last known price stands and `healthy` flips false,
  which is what the header's connection dot reflects. A blank watchlist is worse than a stale one.
- **Backoff caps at 60 s.** On a free key a burst of 429s would otherwise hammer the rate limit
  forever.
- **`sorted()` everywhere the tracked set is iterated.** Deterministic ordering makes seeded
  simulator runs reproducible, which is what `SIM_SEED` is for.

---

## 10. Factory and configuration — `__init__.py`

### 10.1 The two leaf modules

```python
# backend/app/market/symbols.py
from __future__ import annotations

import re

TICKER_RE = re.compile(r"^[A-Z]{1,5}([.-][A-Z]{1,2})?$")


class InvalidTicker(ValueError):
    """Raised at every boundary that accepts a user- or LLM-supplied symbol."""


def normalize_ticker(raw: str) -> str:
    """The ONE normalisation point (D8). Apply at every API boundary, in the LLM action
    executor, and before any cache or DB lookup.

    SQLite's default collation is case-sensitive, so without this `aapl` and `AAPL` become
    two watchlist rows, two cache entries and two positions in the same stock — a heatmap
    showing one holding twice (Review.md B1). Declare the column `COLLATE NOCASE` as a
    backstop, but do not rely on it: the cache and the GBM engine are plain dicts.
    """
    ticker = (raw or "").strip().upper()
    if not TICKER_RE.match(ticker):
        raise InvalidTicker(f"invalid ticker: {raw!r}")
    return ticker
```

```python
# backend/app/market/deps.py
from __future__ import annotations

from fastapi import Request

from .service import MarketDataService


def get_service(request: Request) -> MarketDataService:
    """FastAPI dependency. The service is stored on app.state during lifespan startup."""
    return request.app.state.market
```

The pattern allows `BRK.B` and `RDS-A` alongside plain symbols; all ten seed tickers are ≤ 4
letters. Widen it only with a test.

### 10.2 The factory

```python
# backend/app/market/__init__.py
from __future__ import annotations

import logging
import os
import random

from .cache import PriceCache
from .deps import get_service
from .massive import (FREE_TIER_RPM, PAID_TIER_RPM, MassiveAnchorProvider, MassiveGateway,
                      MassiveLiveSource, probe_entitlement)
from .models import Direction, Quote, Tick
from .routes import router
from .service import MarketDataService, current_session_date
from .simulator import GBMEngine, SimulatedSource, StaticAnchorProvider
from .source import Entitlement, Mode
from .symbols import InvalidTicker, normalize_ticker

log = logging.getLogger(__name__)


async def build_market_service(cache: PriceCache) -> MarketDataService:
    """Assemble the stack from the environment. NEVER raises on a bad market-data key."""
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    sim_seed = os.environ.get("SIM_SEED")
    seed = int(sim_seed) if sim_seed else None

    if not api_key:
        log.info("MASSIVE_API_KEY unset -> SIMULATED mode (static seed prices)")
        return _simulated(cache, StaticAnchorProvider(random.Random(seed)),
                          Mode.SIMULATED, seed)

    gateway = MassiveGateway(api_key, rpm=FREE_TIER_RPM)
    entitlement = await probe_entitlement(gateway)

    if entitlement is Entitlement.SNAPSHOTS:
        gateway.set_rpm(PAID_TIER_RPM)
        interval = float(os.environ.get("MARKET_POLL_INTERVAL_S", "15"))
        log.info("Massive snapshots entitled -> LIVE mode (poll %.1fs)", interval)
        source: object = MassiveLiveSource(gateway, poll_interval=interval)
        if os.environ.get("MARKET_CLOSED_FALLBACK", "true").lower() == "true":
            from .massive import HybridLiveSource
            source = HybridLiveSource(source, SimulatedSource(GBMEngine(seed)), gateway)
        return MarketDataService(source, MassiveAnchorProvider(gateway), cache, Mode.LIVE)

    if entitlement is Entitlement.AGGREGATES:
        log.info("Massive key is aggregates-only (Basic) -> ANCHORED mode: "
                 "real previous closes, simulated intraday motion")
        return _simulated(cache, MassiveAnchorProvider(gateway), Mode.ANCHORED, seed)

    log.warning("Massive key unusable (%s) -> SIMULATED mode", entitlement)
    return _simulated(cache, StaticAnchorProvider(random.Random(seed)), Mode.SIMULATED, seed)


def _simulated(cache: PriceCache, anchors: object, mode: Mode,
               seed: int | None) -> MarketDataService:
    return MarketDataService(
        source=SimulatedSource(GBMEngine(seed)), anchors=anchors, cache=cache, mode=mode,
    )
```

**`ANCHORED` is not a consolation prize.** Positions are marked against genuine market levels — MU
near $877, not a hardcoded $190 from two years ago — while intraday motion comes from the simulator
at the 500 ms cadence PLAN.md §6 requires. A viewer can cross-check MU against any finance site and
find the level right, while the tape moves twice a second.

**Expose the resolved mode.** It appears in the startup log, in `GET /api/health`, and in the SSE
`hello` frame. A user seeing simulated prices deserves to know they are simulated, and it is the
first thing to check when a demo looks wrong.

---

## 11. HTTP surface — `routes.py`

### 11.1 The SSE wire format

This is the highest-traffic interface in the app and the frontend cannot invent it. Fixed here:

- **One event carries every tracked ticker** (D1)
- **Default `message` events — no `event:` line** (D2); the envelope's `type` field discriminates
- **`retry: 1000` first** (D3)
- **Emitted only when `cache.version` advances** (D4), with `: ping` every 15 s otherwise
- **snake_case fields, epoch-ms `ts`** (D14, D17)

First frame after connect (the client can paint immediately from it):

```
retry: 1000

data: {"type":"hello","mode":"anchored","tick_ms":500,"poll_interval_s":0.5,"session_date":"2026-08-12","healthy":true,"quotes":[{"ticker":"ALAB","price":334.17,"prev_price":334.17,"open_price":334.17,"change":0.0,"change_pct":0.0,"direction":"flat","ts":1786538400123}, ...]}

```

Steady state:

```
data: {"type":"prices","seq":1841,"healthy":true,"quotes":[{"ticker":"MU","price":877.7312,"prev_price":877.5108,"open_price":877.57,"change":0.2204,"change_pct":0.019,"direction":"up","ts":1786538412623},{"ticker":"AMD","price":483.2011,"prev_price":483.2604,"open_price":483.36,"change":-0.0593,"change_pct":-0.033,"direction":"down","ts":1786538412623}]}

: ping

```

Client side, one handler, no `addEventListener` wiring:

```javascript
const es = new EventSource('/api/stream/prices');

es.onmessage = (event) => {
  const frame = JSON.parse(event.data);
  if (frame.type === 'hello') {
    setMode(frame.mode);              // "simulated" | "anchored" | "live"
    setSessionDate(frame.session_date);
  }
  setHealthy(frame.healthy);          // drives the connection dot
  applyQuotes(frame.quotes);          // flash on `direction`, label from `change_pct`
};

es.onerror = () => setConnection('reconnecting');   // EventSource retries on its own
```

Because a frame is emitted **only when the cache actually changed**, every `direction` the frontend
sees is a real tick: the flash fires once per price change under the simulator *and* under a 15 s
Massive poll, with no client-side de-duplication. That equivalence is the entire premise of PLAN.md
§6's "one interface".

### 11.2 Implementation

```python
# backend/app/market/routes.py
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .deps import get_service
from .service import MarketDataService
from .symbols import InvalidTicker, normalize_ticker

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["market"])

TICK_INTERVAL = 0.5          # how often the generator CHECKS the cache
KEEPALIVE_S = 15.0           # comment frame to hold the connection through proxies
MAX_HISTORY = 1_000
DEFAULT_SPARK_POINTS = 60


def _frame(payload: dict) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


@router.get("/stream/prices")
async def stream_prices(
    request: Request,
    service: MarketDataService = Depends(get_service),
) -> StreamingResponse:
    return StreamingResponse(
        _price_events(request, service),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",     # stop nginx buffering the stream if proxied
        },
    )


async def _price_events(request: Request, service: MarketDataService):
    cache = service.cache
    client = request.client.host if request.client else "unknown"
    log.info("SSE connect: %s", client)

    yield "retry: 1000\n\n"
    yield _frame({
        "type": "hello",
        "mode": str(service.mode),
        "tick_ms": int(TICK_INTERVAL * 1000),
        "poll_interval_s": service.poll_interval,
        "session_date": service.session_date,
        "healthy": service.healthy,
        "quotes": [q.to_wire() for q in cache.snapshot().values()],
    })

    last_version = cache.version
    last_emit = asyncio.get_running_loop().time()
    try:
        while True:
            if await request.is_disconnected():
                break
            now = asyncio.get_running_loop().time()
            version = cache.version
            if version != last_version:
                last_version = version
                last_emit = now
                yield _frame({
                    "type": "prices",
                    "seq": version,
                    "healthy": service.healthy,
                    "quotes": [q.to_wire() for q in cache.snapshot().values()],
                })
            elif now - last_emit >= KEEPALIVE_S:
                last_emit = now
                yield ": ping\n\n"
            await asyncio.sleep(TICK_INTERVAL)
    except asyncio.CancelledError:
        raise
    finally:
        log.info("SSE disconnect: %s", client)
```

### 11.3 History endpoints

```python
@router.get("/prices/history")
async def bulk_history(
    tickers: str = Query(..., description="Comma-separated symbols"),
    limit: int = Query(DEFAULT_SPARK_POINTS, ge=1, le=MAX_HISTORY),
    service: MarketDataService = Depends(get_service),
) -> dict:
    """Sparkline seeding in ONE call (D6). Ten separate per-ticker requests on mount is
    the alternative, and sparklines need ~60 points, not 1,000."""
    try:
        wanted = [normalize_ticker(t) for t in tickers.split(",") if t.strip()]
    except InvalidTicker as exc:
        raise HTTPException(400, str(exc)) from exc
    # An untracked ticker yields an empty list rather than a 404 — the frontend asks for a
    # whole watchlist at once and one stale symbol must not fail the batch.
    return {
        "series": {
            t: [{"ts": int(ts * 1000), "price": round(p, 4)}
                for ts, p in service.cache.history(t, limit)]
            for t in wanted
        }
    }


@router.get("/prices/{ticker}/history")
async def ticker_history(
    ticker: str,
    limit: int = Query(MAX_HISTORY, ge=1, le=MAX_HISTORY),
    service: MarketDataService = Depends(get_service),
) -> dict:
    """Main-chart seeding: the chart renders populated on first paint instead of filling
    in over minutes (PLAN.md §13 item 2)."""
    symbol = normalize_ticker(ticker)
    if service.quote(symbol) is None:
        raise HTTPException(status_code=404, detail=f"{symbol} is not tracked")
    return {
        "ticker": symbol,
        "points": [{"ts": int(ts * 1000), "price": round(p, 4)}
                   for ts, p in service.cache.history(symbol, limit)],
    }
```

Example:

```bash
$ curl -s 'localhost:8000/api/prices/history?tickers=MU,AMD&limit=3' | jq
{
  "series": {
    "MU":  [{"ts":1786538400123,"price":877.57},
            {"ts":1786538520623,"price":878.1044},
            {"ts":1786538641123,"price":876.9312}],
    "AMD": [{"ts":1786538400123,"price":483.36},
            {"ts":1786538520623,"price":483.9902},
            {"ts":1786538641123,"price":482.7715}]
  }
}
```

Note the timestamps span the whole buffer, not the last 1.5 seconds — that is the subsampling in
`PriceCache.history()` doing its job.

### 11.4 Health

The market fragment merges into `GET /api/health`, which a compose `depends_on: service_healthy`
gate can read:

```json
{
  "status": "ok",
  "database": "ready",
  "market": {
    "mode": "anchored",
    "source": "SimulatedSource",
    "healthy": true,
    "tracked": 11,
    "session_date": "2026-08-12",
    "last_tick_age_s": 0.31,
    "market_status": null
  }
}
```

`market_status` is non-null only under `HybridLiveSource` ([§8.5](#85-optional-closed-market-continuity)).

---

## 12. Integration with the rest of the backend

### 12.1 Lifespan

Ordering is forced by a dependency PLAN.md leaves implicit: **the market service needs the watchlist
to know what to track, so the database must be initialised before the market task starts.** That
settles Review.md D3 — schema init happens in the lifespan startup hook, not on first request.

```python
# backend/app/main.py
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import init_db, tracked_tickers
from app.market import PriceCache, build_market_service
from app.market import router as market_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()                                        # schema + seed (PLAN.md §7)

    cache = PriceCache()
    service = await build_market_service(cache)      # probes Massive, picks the mode
    app.state.market = service

    await service.start(tracked_tickers())           # watchlist ∪ open positions
    try:
        yield
    finally:
        await service.stop()


app = FastAPI(lifespan=lifespan)
app.include_router(market_router)
# ... other routers ...
# StaticFiles(html=True) mounts at "/" LAST — mounting it before the API routers makes
# it swallow /api/* (Review.md C5).
```

```python
# backend/app/db.py  — the union query the tracked set depends on
def tracked_tickers(user_id: str = "default") -> set[str]:
    """PLAN.md §6: watchlist ∪ open positions. Never the watchlist alone."""
    rows = conn.execute(
        """
        SELECT ticker FROM watchlist WHERE user_id = ?
        UNION
        SELECT ticker FROM positions WHERE user_id = ? AND quantity > 1e-9
        """,
        (user_id, user_id),
    ).fetchall()
    return {row["ticker"] for row in rows}
```

### 12.2 Watchlist routes

```python
@router.post("/watchlist", status_code=201)
async def add_watchlist(
    body: WatchlistAdd,
    service: MarketDataService = Depends(get_service),
) -> dict:
    try:
        ticker = normalize_ticker(body.ticker)          # D8
    except InvalidTicker as exc:
        raise HTTPException(400, str(exc)) from exc

    if not await service.validate(ticker):              # D9 — free in Massive modes
        raise HTTPException(400, f"unknown symbol: {ticker}")

    quote = await service.add_ticker(ticker)            # D10 — priced before we return
    if quote is None:
        raise HTTPException(502, f"no market data available for {ticker}")

    db_add_watchlist(ticker)
    return {"ticker": ticker, **quote.to_wire()}


@router.delete("/watchlist/{ticker}", status_code=204)
async def remove_watchlist(
    ticker: str,
    service: MarketDataService = Depends(get_service),
) -> None:
    symbol = normalize_ticker(ticker)
    db_remove_watchlist(symbol)
    await service.sync_tracked(tracked_tickers())       # keeps open positions alive
```

The `DELETE` path is where PLAN.md §13 item 4 lives: `sync_tracked` receives the *union*, so a
ticker with an open position stays in the cache and keeps ticking even though it just left the
watchlist.

### 12.3 Trade execution

```python
async def execute_trade(ticker: str, side: str, quantity: float,
                        service: MarketDataService) -> TradeResult:
    symbol = normalize_ticker(ticker)

    # PLAN.md §9: a ticker outside the watchlist is auto-added — which is also what gives
    # it a price to fill at. add_ticker() returns priced, so there is no wait here.
    quote = service.quote(symbol) or await service.add_ticker(symbol)
    if quote is None:
        return TradeResult(status="rejected", reason=f"no price available for {symbol}")

    fill_price = quote.price
    # ... validate quantity > 0 and finite, cash for buys, shares for sells,
    #     then write trade + position + cash inside ONE transaction, under the
    #     trade asyncio.Lock (Review.md B12/B13) ...

    await service.sync_tracked(tracked_tickers())        # a new position joins the set
    return TradeResult(status="filled", fill_price=fill_price)
```

### 12.4 Portfolio valuation

```python
def position_value(position, service: MarketDataService) -> float:
    """cache.get() returns None before the first tick — fall back to avg_cost rather than
    valuing the position at 0, which would render -100% P&L and write a garbage snapshot
    row that permanently corrupts the P&L chart (D15 / Review.md B16)."""
    price = service.price(position.ticker)
    return position.quantity * (price if price is not None else position.avg_cost)


def should_snapshot(positions, service: MarketDataService) -> bool:
    """Skip the 30s snapshot until every position ticker has a real quote."""
    return all(service.price(p.ticker) is not None for p in positions)
```

---

## 13. Failure modes

| Situation | Behaviour | Where |
|---|---|---|
| `MASSIVE_API_KEY` unset | `SIMULATED` mode, static seed anchors | `build_market_service` |
| Key present, snapshots 403 | `ANCHORED` mode — real closes, simulated motion | `probe_entitlement` |
| Key invalid (401) | Logged, `SIMULATED` mode. **Startup never fails** | `probe_entitlement` |
| Massive unreachable at startup | Grouped walkback returns `{}` → anchors fall back to `SEED_PRICES`, then to `Uniform(40, 400)` | `MassiveAnchorProvider` |
| Poll raises | Last price stands, `healthy=False`, exponential backoff to 60 s | `MarketDataService._run` |
| 429 rate limit | Same backoff path; the token bucket makes it rare | `RateLimiter` |
| Grouped call lands on a weekend/holiday | Walk back up to 7 days from **yesterday** | `_load_universe` |
| Ticker unknown to Massive | `validate()` false → `400` at the API boundary; never a priceless row | D9 |
| Ticker priceless anyway (e.g. IPO'd today) | Skipped from the tracked set with a warning; the route returns `502` | `_track` |
| Trade before the first tick | `add_ticker()` seeds synchronously, so a price always exists | D10 |
| Restart mid-session | Ring buffers empty, anchors re-fetched, daily change restarts at 0.00 % | By design (in-memory) |
| Container running past 09:30 ET | Session roll re-anchors; `ANCHORED` also re-bases the path (reads as an overnight gap) | §9.3 |
| Market closed in `LIVE` mode | `HybridLiveSource` supplies motion; `health.market_status` explains the state | §8.5 |
| SSE client vanishes | `request.is_disconnected()` ends the generator within one tick | §11.2 |
| Empty tracked set (all tickers removed) | Loop idles, no polling, no error; SSE sends `hello` with `quotes: []` | §9.4 |

---

## 14. Testing

Per PLAN.md §12, both implementations must be *shown* to satisfy one interface. **No unit test
touches the network** — the Massive gateway is stubbed everywhere.

```python
# backend/tests/conftest.py
import pytest

from app.market import PriceCache
from app.market.models import Tick
from app.market.simulator import GBMEngine, SimulatedSource, StaticAnchorProvider


@pytest.fixture
def cache() -> PriceCache:
    return PriceCache(history_maxlen=10)


@pytest.fixture
def sim_source() -> SimulatedSource:
    return SimulatedSource(GBMEngine(seed=42))


class FlakySource(SimulatedSource):
    """Raises on every poll — for the resilience test."""

    async def poll(self, tickers):
        raise RuntimeError("provider down")


class StubGateway:
    """Records calls and replays canned responses. No network, no massive package."""

    def __init__(self, responses: dict):
        self.responses, self.calls = responses, []

    async def call(self, fn, /, **kwargs):
        self.calls.append((getattr(fn, "__name__", str(fn)), kwargs))
        result = self.responses[getattr(fn, "__name__", str(fn))]
        if isinstance(result, Exception):
            raise result
        return result
```

### 14.1 Conformance: both sources, one interface

```python
@pytest.mark.parametrize("make_source", [
    lambda: SimulatedSource(GBMEngine(seed=1)),
    lambda: MassiveLiveSource(StubGateway({"list_universal_snapshots": [_snap("MU", 877.57)]})),
])
@pytest.mark.asyncio
async def test_source_conformance(make_source):
    source = make_source()
    await source.prime(["MU"], {"MU": 877.57})

    ticks = await source.poll(["MU"])
    assert [t.ticker for t in ticks] == ["MU"]
    assert ticks[0].price > 0 and math.isfinite(ticks[0].price)

    assert await source.poll([]) == []          # empty request -> empty result
    await source.release("MU")
    await source.release("MU")                  # idempotent
```

### 14.2 Cache invariants

```python
def test_open_price_survives_a_thousand_ticks(cache):
    cache.seed("MU", 877.57, session_date="2026-08-12")
    for i in range(1000):
        cache.apply(Tick("MU", 800.0 + i * 0.1, ts=i))
    quote = cache.get("MU")
    assert quote.open_price == 877.57           # the PLAN.md §13 item 3 invariant
    assert len(quote.history) == 10             # maxlen honoured


def test_day_change_pct_is_exact(cache):
    cache.seed("MU", 100.0)
    cache.apply(Tick("MU", 103.5, ts=1.0))
    assert cache.get("MU").day_change_pct == pytest.approx(3.5)


def test_history_subsamples_across_the_whole_buffer():
    cache = PriceCache(history_maxlen=1000)
    cache.seed("MU", 100.0, ts=0.0)
    for i in range(1, 1000):
        cache.apply(Tick("MU", 100.0 + i, ts=float(i)))
    points = cache.history("MU", limit=60)
    assert len(points) == 60
    assert points[0][0] < 50                    # starts near the beginning, not the end
    assert points[-1] == (999.0, 1099.0)        # always ends on the live price


def test_version_advances_on_every_mutation(cache):
    versions = []
    cache.seed("MU", 100.0); versions.append(cache.version)
    cache.apply(Tick("MU", 101.0, ts=1.0)); versions.append(cache.version)
    cache.evict("MU"); versions.append(cache.version)
    assert versions == sorted(set(versions))    # strictly increasing


def test_apply_on_unseeded_ticker_self_seeds(cache):
    quote = cache.apply(Tick("NVDA", 1200.0, ts=1.0))
    assert quote.open_price == 1200.0 and cache.get("NVDA") is quote
```

### 14.3 Mode detection never raises

```python
@pytest.mark.parametrize("error,expected", [
    (None,                                   Mode.LIVE),
    (Exception("403 NOT_AUTHORIZED"),        Mode.ANCHORED),
    (Exception("401 UNAUTHORIZED"),          Mode.SIMULATED),
    (Exception("connection reset"),          Mode.ANCHORED),   # inconclusive -> safer
])
@pytest.mark.asyncio
async def test_mode_detection(monkeypatch, error, expected):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    ...                                        # patch MassiveGateway with StubGateway
    service = await build_market_service(PriceCache())
    assert service.mode is expected            # and, critically, no exception escaped
```

### 14.4 The tracked-set regression

The one test that protects PLAN.md §13 item 4 — worth writing first because the bug it catches is
silent:

```python
@pytest.mark.asyncio
async def test_position_ticker_keeps_ticking_after_watchlist_removal(sim_source, cache):
    service = MarketDataService(sim_source, StaticAnchorProvider(), cache, Mode.SIMULATED)
    await service.start({"MU", "AMD"})

    # User removes MU from the watchlist but still holds it.
    await service.sync_tracked({"AMD", "MU"})       # union still contains MU
    before = cache.get("MU").price
    await asyncio.sleep(1.2)
    assert cache.get("MU") is not None
    assert cache.get("MU").price != before          # still updating

    # Now the position is closed too -> MU leaves the union and is evicted.
    await service.sync_tracked({"AMD"})
    assert cache.get("MU") is None
    await service.stop()
```

### 14.5 Simulator maths

Full list and rationale in [MARKET_SIMULATOR.md §11](MARKET_SIMULATOR.md). The four that catch real
regressions:

```python
def test_volatility_budget_is_exact():
    engine = GBMEngine(seed=0)
    sigma_d = diffusion_sigma(0.885, engine._jump_variance)
    assert sigma_d**2 + engine._jump_variance == pytest.approx(0.885**2)


def test_diffusion_sigma_clamps_below_the_jump_floor():
    # A low-vol ticker below sigma_jump (0.357) must clamp, not sqrt a negative.
    assert diffusion_sigma(0.10, 0.1278) > 0


def test_realised_volatility_lands_near_target():
    # 60 simulated trading days, measured from daily closes; ±15% keeps RNG variance
    # from flaking the test (practice is ~3% — MARKET_SIMULATOR.md §5).
    assert measure_annualised_sigma("MU", days=60, seed=7) == pytest.approx(0.885, rel=0.15)


def test_correlation_matrix_is_positive_definite_without_the_ridge():
    for tickers in (SEED_WATCHLIST, ["LRCX", "AMAT"] * 12, [f"X{i}" for i in range(50)]):
        assert _cholesky(build_matrix(tickers)) is not None   # catches a bad SECTOR_RHO edit


def test_seeded_runs_are_bit_reproducible():
    a, b = GBMEngine(seed=42), GBMEngine(seed=42)
    for engine in (a, b):
        engine.add_ticker("MU", 877.57)
    assert [a.step() for _ in range(100)] == [b.step() for _ in range(100)]
```

### 14.6 Resilience and the SSE contract

```python
@pytest.mark.asyncio
async def test_failed_polls_preserve_the_last_price_and_flip_healthy(cache):
    service = MarketDataService(FlakySource(GBMEngine(seed=1)), StaticAnchorProvider(),
                                cache, Mode.SIMULATED)
    await service.start({"MU"})
    seeded = cache.get("MU").price
    await asyncio.sleep(2.0)
    assert cache.get("MU").price == seeded       # stale beats blank
    assert service.healthy is False
    assert service._backoff() <= 60.0            # bounded
    await service.stop()


@pytest.mark.asyncio
async def test_stream_emits_hello_then_only_on_change(app_client, cache):
    async with app_client.stream("GET", "/api/stream/prices") as response:
        lines = _collect(response, seconds=1.5)
    assert lines[0] == "retry: 1000"
    hello = json.loads(lines[1].removeprefix("data: "))
    assert hello["type"] == "hello" and hello["mode"] in {"simulated", "anchored", "live"}
    frames = [json.loads(l.removeprefix("data: ")) for l in lines[2:] if l.startswith("data:")]
    assert all(f["seq"] > 0 for f in frames)
    assert [f["seq"] for f in frames] == sorted({f["seq"] for f in frames})  # no repeats
```

---

## 15. Build order

Each step is independently testable; do not start the next until the previous one's tests pass.

1. **`models.py` + `cache.py`** — no dependencies. Land [§14.2](#142-cache-invariants) here; the
   `open_price` invariant is the foundation everything else assumes.
2. **`source.py` + `seeds.py`** — interfaces and data, no logic.
3. **`simulator.py`** — GBM engine and `SimulatedSource`. Land [§14.5](#145-simulator-maths).
   At this point `SIMULATED` mode is fully functional.
4. **`service.py`** — the loop, tracked set, session roll. Land
   [§14.4](#144-the-tracked-set-regression) and [§14.6](#146-resilience-and-the-sse-contract).
5. **`routes.py`** — SSE and history. **The frontend is unblocked here** — it can build against
   `SIMULATED` mode with no Massive key at all.
6. **`massive.py`** — gateway, probe, anchors, live source. `ANCHORED` mode goes live.
   Stub the gateway in tests; verify against the real key manually once.
7. **`__init__.py`** — factory, `normalize_ticker`, dependency. Land
   [§14.3](#143-mode-detection-never-raises).
8. **Integration** — lifespan wiring, then the watchlist/trade call sites in
   [§12](#12-integration-with-the-rest-of-the-backend).

Dependencies to add: `massive` (the SDK) and `tzdata` (so `ZoneInfo("America/New_York")` resolves on
Windows dev machines and slim images). And the landmine from the previous build
([Review.md C4](Review.md)) — without this `uv sync` fails outright with *"Unable to determine which
files to ship inside the wheel"*:

```toml
[tool.hatch.build.targets.wheel]
packages = ["app"]
```

---

## Appendix A — worked example: a session on this repo's key

What an implementer should actually observe on first run, given `.env` carries a populated Basic-tier
`MASSIVE_API_KEY`.

**Startup (4 requests total, then zero):**

```
INFO  app.market            Massive key is aggregates-only (Basic) -> ANCHORED mode:
                            real previous closes, simulated intraday motion
INFO  app.market.massive    Massive grouped session 2026-08-11: 12414 symbols
INFO  app.market.service    market data: mode=anchored source=SimulatedSource
                            tracking=10 session=2026-08-12
INFO  app.market.routes     SSE connect: 172.17.0.1
```

The four requests: one entitlement probe (403, expected), one grouped-daily call that returns 12,414
symbols — which resolves all ten anchors *and* becomes the symbol-validation universe — and up to
two walkback retries if yesterday was a weekend.

**Anchors resolved, watchlist priced from the first frame:**

```python
{"ALAB": 334.17, "MRVL": 218.72, "MU": 877.57, "AMD": 483.36, "INTC": 101.65,
 "PLTR": 172.01, "ANET": 188.67, "LRCX": 311.35, "AMAT": 539.14, "SLV": 57.50}
```

Every daily change % starts at exactly 0.00 % and drifts from there. A viewer can check MU against
any finance site and find the level right.

**Steady state:** zero Massive requests. The engine steps every 500 ms; the cache version advances;
the SSE generator emits. `LRCX` and `AMAT` visibly move together (ρ = 0.90) while `PLTR` and `SLV`
wander independently — the correlation structure is the detail that makes the watchlist read as a
market rather than ten random walks.

**User adds `PYPL` via the chat panel:**

```
POST /api/watchlist {"ticker":" pypl "}
  -> normalize_ticker      -> "PYPL"
  -> service.validate      -> True   (found in the cached universe — 0 requests)
  -> service.add_ticker    -> anchor 72.14 from the same cached universe (0 requests)
                              cache.seed, engine.add_ticker, one GBM step
  -> 201 {"ticker":"PYPL","price":72.1409,"open_price":72.14,"change_pct":0.001,...}
```

Total cost: **zero API requests**, and the response already carries a price, so the LLM's very next
step can fill a trade against it.

**Next morning, 09:30 ET, container still running:**

```
INFO  app.market.service   session roll: 2026-08-12 -> 2026-08-13 (mode=anchored)
INFO  app.market.massive   Massive grouped session 2026-08-12: 12409 symbols
```

Anchors refresh to the new real closes; each simulated path re-bases onto its close. The
discontinuity in the chart is an overnight gap — which is exactly what a real chart shows.

---

## Appendix B — configuration summary

| Variable | Default | Effect |
|---|---|---|
| `MASSIVE_API_KEY` | *(unset)* | Unset → `SIMULATED`. Set → probe decides `ANCHORED` or `LIVE`. Never fails startup. |
| `SIM_SEED` | *(unset)* | Seeds the simulator RNG for reproducible E2E runs (Review.md B10) |
| `MARKET_POLL_INTERVAL_S` | `15` | `LIVE` poll cadence. Free-tier-safe at 15 s; 2 s on Advanced. Ignored in simulated modes. |
| `MARKET_CLOSED_FALLBACK` | `true` | `LIVE` only: keep the tape moving outside market hours ([§8.5](#85-optional-closed-market-continuity)) |

**Not configurable, deliberately:** the mode. It is detected from what the key can actually do; an
override would only let a deployment lie about its entitlement.

| Constant | Value | Where | Source |
|---|---|---|---|
| SSE check cadence | 500 ms | `routes.TICK_INTERVAL` | PLAN.md §6 |
| SSE keepalive | 15 s | `routes.KEEPALIVE_S` | Review A3 |
| `retry:` directive | 1000 ms | `routes._price_events` | Review A2 |
| History ring buffer | 1,000 points | `cache.HISTORY_MAXLEN` | PLAN.md §6 |
| Sparkline default | 60 points | `routes.DEFAULT_SPARK_POINTS` | Review A4 |
| Simulator tick | 500 ms | `simulator.TICK_SECONDS` | PLAN.md §6 |
| `dt` | 8.4792e-08 | `0.5 / (252 × 6.5 × 3600)` | MARKET_SIMULATOR.md §1 |
| `JUMP_PROB` | 1e-4 | ≈ 4.7 events/ticker/day | MARKET_SIMULATOR.md §5 |
| Jump magnitude | ±0.5 %–1.5 % | `simulator.JUMP_MIN/MAX` | MARKET_SIMULATOR.md §5 |
| Free-tier rate limit | 5 req/min | `massive.FREE_TIER_RPM` | MASSIVE_API.md §2 |
| Grouped walkback | 7 days, from **yesterday** | `massive.GROUPED_LOOKBACK_DAYS` | §8.3 |
| Session boundary | 09:30 America/New_York | `service.SESSION_OPEN` | Review B4 |
| Unhealthy threshold | 3 consecutive failures | `service.UNHEALTHY_AFTER` | §9.4 |
| Max backoff | 60 s | `service.MAX_BACKOFF` | §9.4 |
