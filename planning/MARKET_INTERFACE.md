# Market Data Interface Design

The unified Python interface FinAlly uses to retrieve stock prices. One abstraction, selected at
startup from the environment, with every downstream consumer — SSE streaming, portfolio valuation,
trade fills, the LLM's portfolio context — source-agnostic.

Companions: [MASSIVE_API.md](MASSIVE_API.md) (the provider) and
[MARKET_SIMULATOR.md](MARKET_SIMULATOR.md) (the price-generation model).

---

## 1. The constraint that shapes this design

PLAN.md §5 specifies a binary switch:

> If `MASSIVE_API_KEY` is set and non-empty → backend uses Massive REST API for market data.
> If absent or empty → backend uses the built-in market simulator.

Live probing (logged in [MASSIVE_API.md §9](MASSIVE_API.md#9-verification-log)) shows that binary is
too coarse. **This project's key is on Massive's Basic tier, where both snapshot endpoints return
HTTP 403 and no current-day data is available at all.** A key being present does not imply live
prices are obtainable.

Taking PLAN.md §5 literally would produce a workstation that streams *nothing* — the very failure
mode §5 was written to prevent. So the switch becomes three-way, and the third mode is the good one:

| Mode | Trigger | Prices | Anchors (`open_price`) |
|---|---|---|---|
| `SIMULATED` | No key | Simulated | Static seed table |
| `ANCHORED` | Key present, snapshots 403 (**Basic — this repo**) | Simulated intraday | **Real** previous closes from Massive |
| `LIVE` | Key present, snapshots entitled (Starter+) | **Real**, polled | Real session data |

`ANCHORED` is not a consolation prize. Positions are marked against genuine market levels — MU near
$877, not a hardcoded $190 from two years ago — while intraday motion comes from the simulator at
the 500 ms cadence PLAN.md §6 requires. A free key buys real *price levels*; the simulator supplies
the *motion* the free tier cannot.

The mode is **detected, never configured**. A `MODE` env var would just let the deployment lie about
what the key can do.

---

## 2. Data model

Two types cross the market-data boundary. Nothing downstream touches a provider SDK object.

```python
from dataclasses import dataclass, field
from typing import Literal

Direction = Literal["up", "down", "flat"]


@dataclass(frozen=True)
class Tick:
    """One price observation. What a source produces."""
    ticker: str
    price: float
    ts: float                    # Unix seconds (float), normalised at the adapter


@dataclass
class Quote:
    """Cached state for one ticker. Mirrors the PLAN.md §6 cache entry."""
    ticker: str
    price: float
    prev_price: float
    open_price: float            # session anchor; set once, never overwritten by ticks
    ts: float
    history: deque[tuple[float, float]]   # bounded ring buffer of (ts, price)

    @property
    def change(self) -> float:
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
        if not self.open_price:
            return 0.0
        return (self.price - self.open_price) / self.open_price * 100.0
```

`direction` drives the green/red flash; `day_change_pct` drives the watchlist's daily change column.
They answer different questions and must not be conflated — PLAN.md §6 is explicit that `prev_price`
is the last *tick*, so differencing against it yields a per-tick delta near zero, not a daily move.

**Timestamps are float Unix seconds everywhere inside FinAlly.** Massive mixes milliseconds
(aggregates `t`) and nanoseconds (`updated`, `sip_timestamp`); each adapter normalises at its own
edge so no provider unit ever reaches the cache.

---

## 3. The two abstractions

The archived design gave each source its own `start`/`stop`/polling loop, which duplicated the loop
in every implementation and left the simulator and the poller free to drift apart. This design
splits the concern in two: **sources produce prices, one service owns the loop.**

### 3.1 `AnchorProvider` — where does a ticker's session baseline come from?

```python
from typing import Protocol


class AnchorProvider(Protocol):
    """Supplies the session anchor price for tickers entering the tracked set."""

    async def anchors(self, tickers: list[str]) -> dict[str, float]:
        """Return {ticker: anchor_price}. Omit tickers that cannot be resolved."""
```

Two implementations:

- **`StaticAnchorProvider`** — the seed table in [MARKET_SIMULATOR.md](MARKET_SIMULATOR.md). No
  network. Unknown tickers get a plausible random level.
- **`MassiveAnchorProvider`** — one `get_grouped_daily_aggs` call resolves *every* ticker at once
  (see [MASSIVE_API.md §5.1](MASSIVE_API.md#51-daily-market-summary-grouped--the-free-tier-workhorse)),
  which is what makes this affordable on a 5-req/min key. Falls back to
  `get_previous_close_agg` for a single ticker added mid-session, and to the static table if
  Massive has no row for the symbol.

### 3.2 `MarketDataSource` — where does the next price come from?

```python
from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Produces prices for a set of tickers. Owns no loop and no cache."""

    #: How often the service should call `poll()`, in seconds.
    poll_interval: float

    @abstractmethod
    async def prime(self, tickers: list[str], anchors: dict[str, float]) -> None:
        """Register tickers and their session anchors before the first poll."""

    @abstractmethod
    async def poll(self, tickers: list[str]) -> list[Tick]:
        """Return the current price for each ticker. Called on `poll_interval`."""

    @abstractmethod
    async def release(self, ticker: str) -> None:
        """Drop any per-ticker state. Called when a ticker leaves the tracked set."""

    async def aclose(self) -> None:
        """Release process-wide resources. Default: nothing."""
```

`poll()` is total and stateless from the caller's view: hand it tickers, get ticks back. No
implementation spawns a task, sleeps, or writes to the cache.

---

## 4. Implementations

### `SimulatedSource` — used by both `SIMULATED` and `ANCHORED`

```python
class SimulatedSource(MarketDataSource):
    poll_interval = 0.5

    def __init__(self, engine: GBMEngine) -> None:
        self._engine = engine

    async def prime(self, tickers: list[str], anchors: dict[str, float]) -> None:
        for t in tickers:
            self._engine.add_ticker(t, start_price=anchors[t])

    async def poll(self, tickers: list[str]) -> list[Tick]:
        now = time.time()
        return [Tick(t, p, now) for t, p in self._engine.step().items()]

    async def release(self, ticker: str) -> None:
        self._engine.remove_ticker(ticker)
```

The **only** difference between `SIMULATED` and `ANCHORED` is which `AnchorProvider` fills
`anchors`. Same source class, same engine, same cadence — the mode changes where the starting prices
come from, nothing else. That is why `ANCHORED` costs almost no extra code.

### `MassiveLiveSource` — used by `LIVE` (Starter+ only)

```python
class MassiveLiveSource(MarketDataSource):
    def __init__(self, client: RESTClient, poll_interval: float = 15.0) -> None:
        self._client = client
        self.poll_interval = poll_interval

    async def prime(self, tickers: list[str], anchors: dict[str, float]) -> None:
        return None                      # the API is authoritative; nothing to seed

    async def poll(self, tickers: list[str]) -> list[Tick]:
        snapshots = await asyncio.to_thread(          # client is sync/urllib3
            self._client.get_snapshot_all,
            market_type="stocks",
            tickers=tickers,
        )
        ticks = []
        for s in snapshots:
            price = s.last_trade.price if s.last_trade else (s.day.close if s.day else None)
            if price:
                ticks.append(Tick(s.ticker, price, _ns_to_s(s.updated)))
        return ticks

    async def release(self, ticker: str) -> None:
        return None                      # stateless
```

`poll_interval` defaults to 15 s, the free-tier-safe cadence PLAN.md §6 names; on Advanced it can
drop to 2 s. Note the poll interval and the **SSE cadence are independent** — SSE always emits at
~500 ms from the cache (§6), so a 15 s poll produces a stepped chart rather than a stalled one.

---

## 5. Price cache

```python
from collections import deque

HISTORY_MAXLEN = 1_000        # PLAN.md §6: ~8 min at 500ms


class PriceCache:
    """In-memory quote store. Single source of truth for 'what is X worth right now'."""

    def __init__(self, history_maxlen: int = HISTORY_MAXLEN) -> None:
        self._quotes: dict[str, Quote] = {}
        self._maxlen = history_maxlen

    def seed(self, ticker: str, anchor: float, ts: float | None = None) -> Quote:
        """Create the entry and fix its `open_price`. Idempotent."""
        if ticker in self._quotes:
            return self._quotes[ticker]
        ts = ts or time.time()
        q = Quote(
            ticker=ticker, price=anchor, prev_price=anchor, open_price=anchor,
            ts=ts, history=deque([(ts, anchor)], maxlen=self._maxlen),
        )
        self._quotes[ticker] = q
        return q

    def apply(self, tick: Tick) -> Quote:
        """Record a new price. Never touches `open_price`."""
        q = self._quotes.get(tick.ticker)
        if q is None:
            return self.seed(tick.ticker, tick.price, tick.ts)
        q.prev_price = q.price
        q.price = tick.price
        q.ts = tick.ts
        q.history.append((tick.ts, tick.price))
        return q

    def get(self, ticker: str) -> Quote | None:
        return self._quotes.get(ticker)

    def snapshot(self) -> dict[str, Quote]:
        return dict(self._quotes)

    def history(self, ticker: str) -> list[tuple[float, float]]:
        q = self._quotes.get(ticker)
        return list(q.history) if q else []

    def evict(self, ticker: str) -> None:
        self._quotes.pop(ticker, None)
```

`seed()` and `apply()` are deliberately separate. `open_price` is written **once**, at seed time, and
no tick may overwrite it — that invariant is the whole reason daily change % is computable
(PLAN.md §13 item 3).

### Concurrency

**All cache mutation happens on the event-loop thread.** Only the blocking HTTP call is offloaded
(`asyncio.to_thread` inside `poll()`); the returned ticks are applied back on the loop. Under
PLAN.md §3's single-worker model that makes the cache single-threaded, so it needs no lock.

The archived design wrapped every access in a `threading.Lock`, which is dead weight under this
invariant — and, worse, implies a thread-safety guarantee the rest of the system does not actually
maintain. If a future change writes to the cache from a real thread, add the lock back **and** say so
here; do not leave the two designs half-merged.

---

## 6. `MarketDataService` — the single owner

One class owns the loop, the tracked set, the cache, and the mode. It is the only thing the FastAPI
app talks to.

```python
class MarketDataService:
    def __init__(
        self,
        source: MarketDataSource,
        anchors: AnchorProvider,
        cache: PriceCache,
        mode: Mode,
    ) -> None:
        self._source, self._anchors, self._cache, self.mode = source, anchors, cache, mode
        self._tracked: set[str] = set()
        self._task: asyncio.Task | None = None
        self._last_ok: float = 0.0
        self._consecutive_failures = 0

    # ---- lifecycle -------------------------------------------------
    async def start(self, tickers: set[str]) -> None:
        await self._track(tickers)
        self._task = asyncio.create_task(self._run(), name="market-data")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._source.aclose()

    # ---- tracked set: watchlist ∪ open positions -------------------
    async def sync_tracked(self, tickers: set[str]) -> None:
        """Recompute the tracked set. Call after any watchlist or position change."""
        added, removed = tickers - self._tracked, self._tracked - tickers
        if added:
            await self._track(added)
        for t in removed:
            self._tracked.discard(t)
            await self._source.release(t)
            self._cache.evict(t)

    async def _track(self, tickers: set[str]) -> None:
        new = sorted(tickers - self._tracked)
        if not new:
            return
        resolved = await self._anchors.anchors(new)
        for t in new:
            anchor = resolved.get(t)
            if anchor is None:
                log.warning("no anchor for %s; skipping", t)
                continue
            self._cache.seed(t, anchor)
            self._tracked.add(t)
        await self._source.prime(sorted(self._tracked & set(new)), resolved)

    # ---- the one polling loop --------------------------------------
    async def _run(self) -> None:
        while True:
            try:
                if self._tracked:
                    for tick in await self._source.poll(sorted(self._tracked)):
                        if tick.ticker in self._tracked:      # guard against races
                            self._cache.apply(tick)
                    self._last_ok = time.time()
                    self._consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                self._consecutive_failures += 1
                log.exception("market poll failed (%d consecutive)", self._consecutive_failures)
            await asyncio.sleep(self._backoff())

    def _backoff(self) -> float:
        base = self._source.poll_interval
        if not self._consecutive_failures:
            return base
        return min(base * 2 ** min(self._consecutive_failures, 5), 60.0)

    @property
    def healthy(self) -> bool:
        return self._consecutive_failures < 3
```

Three details that matter:

- **The tracked set is `watchlist ∪ open positions`** (PLAN.md §6, §13 item 4). A ticker removed
  from the watchlist while a position is open must keep updating, or portfolio value, the heatmap,
  and the P&L chart all silently go stale. Callers pass the union; `sync_tracked` is invoked from the
  watchlist endpoints *and* from trade execution.
- **A failed poll never clears the cache.** The last known price stands and `healthy` goes false,
  which is what the header's connection dot reflects. A blank watchlist is worse than a stale one.
- **Exponential backoff caps at 60 s.** On a free key a burst of 429s would otherwise hammer the
  rate limit forever.

---

## 7. Factory and mode detection

```python
class Mode(StrEnum):
    SIMULATED = "simulated"
    ANCHORED = "anchored"
    LIVE = "live"


async def build_market_service(cache: PriceCache) -> MarketDataService:
    """Assemble the market data stack from the environment. Never raises on a bad key."""
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if not api_key:
        log.info("MASSIVE_API_KEY unset -> SIMULATED mode")
        return _simulated(cache, StaticAnchorProvider(), Mode.SIMULATED)

    client = RESTClient(api_key=api_key)
    entitlement = await probe_entitlement(client)

    if entitlement is Entitlement.SNAPSHOTS:
        log.info("Massive snapshots entitled -> LIVE mode")
        return MarketDataService(
            source=MassiveLiveSource(client),
            anchors=MassiveAnchorProvider(client),
            cache=cache, mode=Mode.LIVE,
        )

    if entitlement is Entitlement.AGGREGATES:
        log.info("Massive key is aggregates-only (Basic) -> ANCHORED mode: "
                 "real closes, simulated intraday")
        return _simulated(cache, MassiveAnchorProvider(client), Mode.ANCHORED)

    log.warning("Massive key unusable (%s) -> SIMULATED mode", entitlement)
    return _simulated(cache, StaticAnchorProvider(), Mode.SIMULATED)


def _simulated(cache, anchors, mode) -> MarketDataService:
    return MarketDataService(
        source=SimulatedSource(GBMEngine()), anchors=anchors, cache=cache, mode=mode,
    )
```

The probe costs one request and runs once per process:

```python
class Entitlement(StrEnum):
    SNAPSHOTS = "snapshots"     # Starter+
    AGGREGATES = "aggregates"   # Basic
    NONE = "none"               # bad key / unreachable


async def probe_entitlement(client: RESTClient) -> Entitlement:
    try:
        await asyncio.to_thread(
            client.get_snapshot_all, market_type="stocks", tickers=["AAPL"]
        )
        return Entitlement.SNAPSHOTS
    except Exception as exc:
        text = str(exc)
        if "NOT_AUTHORIZED" in text or "403" in text:
            return Entitlement.AGGREGATES
        if "401" in text or "UNAUTHORIZED" in text:
            return Entitlement.NONE
        log.warning("entitlement probe inconclusive: %s", exc)
        return Entitlement.AGGREGATES     # aggregates are the safer assumption
```

**A bad market-data key must never crash startup.** This is the deliberate opposite of PLAN.md §5's
`OPENROUTER_API_KEY` rule, and the asymmetry is intentional: chat is unusable without its key, but
market data always has a working fallback, so degrading beats failing.

Expose the resolved mode on `GET /api/health` and in the SSE `hello` event — a user seeing simulated
prices deserves to know they are simulated, and it is the first thing to check when a demo looks
wrong.

---

## 8. Consumers

### SSE stream (`GET /api/stream/prices`)

Reads the cache on a fixed 500 ms cadence, independent of `poll_interval`:

```python
async def price_events(service: MarketDataService, cache: PriceCache):
    yield _sse("hello", {"mode": service.mode, "interval_ms": 500})
    while True:
        payload = [
            {
                "ticker": q.ticker,
                "price": round(q.price, 4),
                "prev_price": round(q.prev_price, 4),
                "open_price": round(q.open_price, 4),
                "change_pct": round(q.day_change_pct, 3),
                "direction": q.direction,
                "ts": q.ts,
            }
            for q in cache.snapshot().values()
        ]
        yield _sse("prices", {"quotes": payload, "healthy": service.healthy})
        await asyncio.sleep(0.5)
```

The event carries ticker, price, previous price, open price, timestamp, and direction exactly as
PLAN.md §6 specifies, plus the precomputed `change_pct` so the frontend never re-derives the anchor
maths, and `healthy` to drive the connection dot.

### Price history (`GET /api/prices/{ticker}/history`)

`cache.history(ticker)` → `[{"ts": ..., "price": ...}]`, so charts render populated on first paint
(PLAN.md §13 item 2). In-memory and restart-transient by design.

### Trade fills and valuation

`cache.get(ticker).price` is the fill price. Trade execution must call
`service.sync_tracked(watchlist | open_positions)` afterwards so a newly bought ticker starts
updating — including the LLM's auto-add path in PLAN.md §9.

---

## 9. File structure

```
backend/app/market/
├── __init__.py          # build_market_service, Mode — the public surface
├── models.py            # Tick, Quote, Direction
├── cache.py             # PriceCache
├── service.py           # MarketDataService
├── source.py            # MarketDataSource ABC, AnchorProvider protocol
├── simulator.py         # GBMEngine, SimulatedSource   (MARKET_SIMULATOR.md)
├── massive.py           # MassiveLiveSource, MassiveAnchorProvider, probe_entitlement
└── seeds.py             # SEED_PRICES, TICKER_PARAMS, correlation groups
```

Only `__init__.py` is imported outside `app/market/`. Routes receive the service and cache by
dependency injection; nothing else constructs a `RESTClient`.

---

## 10. Lifecycle

1. **Startup** (FastAPI lifespan) — create `PriceCache`; `await build_market_service(cache)`; read
   the watchlist ∪ open positions from SQLite; `await service.start(tracked)`. Anchors resolve in
   one Massive call, so the first SSE frame already carries real levels.
2. **Watchlist add/remove** → `await service.sync_tracked(...)`.
3. **Trade executed** → `await service.sync_tracked(...)`, then snapshot the portfolio.
4. **SSE connect** → new generator reading the shared cache; no per-client polling.
5. **Shutdown** → `await service.stop()`.

---

## 11. Testing

Per PLAN.md §12, both implementations must be shown to satisfy one interface.

- **Conformance** — parametrise one test suite over `SimulatedSource` and `MassiveLiveSource` (the
  latter against a stubbed `RESTClient`): `poll()` returns a `Tick` per requested ticker, prices are
  positive and finite, `release()` is idempotent, `poll()` on an empty list returns `[]`.
- **Mode detection** — stub the client to raise 403 → `ANCHORED`; 401 → `SIMULATED`; succeed →
  `LIVE`. Assert startup never raises.
- **Cache invariants** — `open_price` survives a thousand `apply()` calls; `history` never exceeds
  `maxlen`; `day_change_pct` is exact against hand-computed values; `apply()` on an unseeded ticker
  self-seeds rather than raising.
- **Tracked set** — a ticker removed from the watchlist but held as a position stays in the cache
  and keeps ticking. This is PLAN.md §13 item 4 and is worth an explicit regression test.
- **Resilience** — a source raising on every poll leaves the last price intact, flips `healthy`
  false, and backs off without unbounded growth.

Anchor providers are stubbed in every test but the Massive-specific ones; no unit test touches the
network.
