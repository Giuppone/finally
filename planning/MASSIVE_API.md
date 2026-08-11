# Massive API Reference (formerly Polygon.io)

Reference for the Massive REST API as FinAlly uses it: fetching prices for many tickers, both
intraday/real-time and end-of-day.

Polygon.io rebranded to Massive on **30 October 2025**. The API surface, paths, and response shapes
are unchanged — only the hostname, the PyPI package, and the env var name moved.

> **Everything in the "Verified" column of the entitlement table below was probed live against this
> repo's `MASSIVE_API_KEY` on 2026-08-10.** Plan entitlements are the single biggest source of
> surprise with this API, so they were tested rather than assumed. See
> [Entitlements](#4-entitlements--what-your-key-can-actually-call).

---

## 1. Connection basics

| Item | Value |
|---|---|
| Base URL | `https://api.massive.com` |
| Legacy base URL | `https://api.polygon.io` (still routes; docs examples mix both) |
| Python package | `massive` (v2.8.0, May 2026) — `uv add massive` |
| Python support | `>=3.9, <4.0` |
| Client classes | `RESTClient`, `WebSocketClient` |
| Env var | `MASSIVE_API_KEY` |
| Auth header | `Authorization: Bearer <API_KEY>` |
| Auth alternative | `?apiKey=<API_KEY>` query param |
| Source | [github.com/massive-com/client-python](https://github.com/massive-com/client-python) |

### Client construction

The `RESTClient` constructor, verbatim from `massive/rest/__init__.py`:

```python
def __init__(
    self,
    api_key: Optional[str] = os.getenv(ENV_KEY),   # ENV_KEY == "MASSIVE_API_KEY"
    connect_timeout: float = 10.0,
    read_timeout: float = 10.0,
    num_pools: int = 10,
    retries: int = 3,
    base: str = BASE,                              # "https://api.massive.com"
    pagination: bool = True,
    verbose: bool = False,
    trace: bool = False,
    custom_json: Optional[Any] = None,
):
```

Two things worth noting for FinAlly:

- **The default `api_key` reads `MASSIVE_API_KEY` from the environment at import time.** PLAN.md §5
  says the backend reads config from `os.environ` only, which this satisfies — but pass the key
  explicitly anyway so the dependency is visible and testable.
- **`retries=3` is on by default**, and the client blocks while retrying. Combined with the 10s read
  timeout, one bad call can stall a coroutine for ~30s. Always call it via `asyncio.to_thread`
  (§7) — the client is synchronous, built on `urllib3`.

```python
from massive import RESTClient

client = RESTClient(api_key=api_key)  # explicit beats implicit
```

---

## 2. Rate limits

| Tier | Limit |
|---|---|
| Basic (free) | **5 requests/minute** |
| Starter / Developer / Advanced / Business | Unlimited (stay under ~100 req/s) |

Exceeding the free limit returns **HTTP 429**. Five calls a minute is the constraint that shapes the
whole polling design: with a 10-ticker watchlist, any *per-ticker* endpoint is unusable on the free
tier (10 calls > 5/min). Only the **multi-ticker-in-one-call** endpoints are viable — see §5.

---

## 3. Pricing tiers (stocks, individual plans)

| Plan | Price/mo | Recency | History |
|---|---|---|---|
| Basic | Free | **End-of-day** | 2 years |
| Starter | $29 | 15-min delayed | 5 years |
| Developer | $79 | 15-min delayed (+ trades) | 10 years |
| Advanced | $199 | **Real-time** (+ WebSocket) | 15+ years |

"End-of-day" on Basic is not a footnote — it means the free tier returns **no current-day data at
all**. Requesting today's bars yields `Your plan doesn't include this data timeframe`.

---

## 4. Entitlements — what your key can actually call

This is the part that breaks naive implementations. Endpoints do not fail with a friendly empty
result; they return **HTTP 403 `NOT_AUTHORIZED`**.

Probed live on 2026-08-10 against this repo's key:

| Endpoint | Path | Min plan | This key (Basic) |
|---|---|---|---|
| Full Market Snapshot | `/v2/snapshot/locale/us/markets/stocks/tickers` | Starter | ❌ **403** |
| Unified Snapshot | `/v3/snapshot` | Starter | ❌ **403** |
| Last Trade | `/v2/last/trade/{ticker}` | Developer | ❌ **403** |
| Previous Day Bar | `/v2/aggs/ticker/{ticker}/prev` | **Basic** | ✅ **200** |
| Daily Market Summary (grouped) | `/v2/aggs/grouped/locale/us/market/stocks/{date}` | **Basic** | ✅ **200** |
| Custom Bars | `/v2/aggs/ticker/{t}/range/{mult}/{span}/{from}/{to}` | **Basic** | ✅ **200** |
| Market Status | `/v1/marketstatus/now` | **Basic** | ✅ (all plans, real-time) |

The exact 403 body:

```json
{
  "status": "NOT_AUTHORIZED",
  "request_id": "d49fca3119704663c06007413d38349a",
  "message": "You are not entitled to this data. Please upgrade your plan at https://massive.com/pricing"
}
```

### What this means for FinAlly

**The free tier cannot stream live prices.** Both snapshot endpoints — the ones every "poll for
current prices" tutorial reaches for — are Starter+. On a Basic key the newest datum obtainable is
**the previous session's close**.

This is a hard constraint, not a tuning problem, and it drives the three-mode design in
[MARKET_INTERFACE.md](MARKET_INTERFACE.md): a Basic key is used to fetch *real anchor prices*, and
intraday motion is simulated from them. Detect the tier at startup by probing (§8) rather than
trusting configuration.

---

## 5. Endpoints

### 5.1 Daily Market Summary (grouped) — the free-tier workhorse

Every US ticker's OHLCV for one date, in **one request**. The only endpoint that gives many tickers'
prices in a single free-tier call, so it is FinAlly's EOD source.

```
GET /v2/aggs/grouped/locale/us/market/stocks/{date}
```

| Param | Type | Description |
|---|---|---|
| `date` | string, required | `YYYY-MM-DD`. Must be a **trading day** — weekends/holidays return an empty `results`. |
| `adjusted` | boolean | Adjust for splits. Default `true`. |
| `include_otc` | boolean | Include OTC. Default `false`. |

```python
from massive import RESTClient

client = RESTClient(api_key=api_key)

# One call -> every US ticker for that session.
bars = client.get_grouped_daily_aggs(date="2026-08-07", adjusted=True)

wanted = {"ALAB", "MRVL", "MU", "AMD", "INTC", "PLTR", "ANET", "LRCX", "AMAT", "SLV"}
prices = {b.ticker: b.close for b in bars if b.ticker in wanted}
print(prices)
```

Raw REST equivalent and a real (truncated) response:

```bash
curl -H "Authorization: Bearer $MASSIVE_API_KEY" \
  "https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/2026-08-07?adjusted=true"
```

```json
{
  "queryCount": 12414,
  "resultsCount": 12414,
  "adjusted": true,
  "status": "OK",
  "results": [
    {"T": "XLRE", "v": 3863635.4, "vw": 44.9929, "o": 44.83, "c": 44.98,
     "h": 45.255, "l": 44.71, "t": 1786132800000, "n": 26295}
  ]
}
```

**Field key** (shared by every aggregate endpoint — these one-letter names are the API's, not a
shorthand invented here):

| Field | Meaning |
|---|---|
| `T` | Ticker (grouped endpoint only) |
| `o` `h` `l` `c` | Open, high, low, close |
| `v` | Volume |
| `vw` | Volume-weighted average price |
| `t` | Unix **millisecond** timestamp of the bar's start |
| `n` | Number of transactions |

12,414 tickers came back in that one call — filter client-side. It is a ~3-5 MB response, so fetch
it once per session and cache, never per request.

### 5.2 Previous Day Bar

Previous session's OHLCV for **one** ticker. Free-tier accessible, but one call per ticker — at
5 req/min a 10-ticker watchlist takes over two minutes. Use §5.1 instead for bulk; use this for a
single ticker added mid-session.

```
GET /v2/aggs/ticker/{stocksTicker}/prev
```

```python
prev = client.get_previous_close_agg(ticker="AAPL", adjusted=True)
for agg in prev:                       # yes, iterate — see the caveat below
    print(agg.ticker, agg.open, agg.high, agg.low, agg.close, agg.volume, agg.vwap)
```

> ⚠️ **The return type annotation is wrong.** `get_previous_close_agg` is annotated
> `-> Union[PreviousCloseAgg, HTTPResponse]`, implying a single object, but the API's `results` is an
> array and `BaseClient._get` maps the deserializer over lists
> (`obj = [deserializer(o) for o in obj]`). At runtime you get a **`list[PreviousCloseAgg]`**.
> Iterate it, and expect mypy to disagree. `PreviousCloseAgg` fields: `ticker`, `open`, `high`,
> `low`, `close`, `volume`, `vwap`, `timestamp`.

Real response (2026-08-10):

```json
{
  "ticker": "AAPL",
  "queryCount": 1, "resultsCount": 1, "adjusted": true, "status": "OK",
  "results": [
    {"T": "AAPL", "v": 44812494, "vw": 307.2588, "o": 306.83, "c": 308.26,
     "h": 308.26, "l": 304.61, "t": 1786392000000, "n": 925003}
  ]
}
```

### 5.3 Custom Bars (aggregates)

Historical OHLCV over a date range at any resolution. FinAlly uses it to calibrate simulator
volatility (see [MARKET_SIMULATOR.md](MARKET_SIMULATOR.md)) and, optionally, to seed the chart ring
buffer with a real intraday shape.

```
GET /v2/aggs/ticker/{stocksTicker}/range/{multiplier}/{timespan}/{from}/{to}
```

| Param | Description |
|---|---|
| `multiplier` | Size of the timespan multiplier (e.g. `1`, `5`) |
| `timespan` | `second`, `minute`, `hour`, `day`, `week`, `month`, `quarter`, `year` |
| `from` / `to` | `YYYY-MM-DD` or millisecond timestamp |
| `adjusted` | Split-adjust. Default `true` |
| `sort` | `asc` or `desc` |
| `limit` | Default 5000, max 50000 |

```python
bars = []
for bar in client.list_aggs(
    ticker="MU",
    multiplier=1,
    timespan="day",
    from_="2025-12-01",   # trailing underscore: `from` is a Python keyword
    to="2026-08-07",
    adjusted=True,
    sort="asc",
    limit=50000,
):
    bars.append(bar)

closes = [b.close for b in bars]
```

`list_aggs` is a generator and **auto-paginates** (`pagination=True` by default). On a free key a
wide range can therefore fan out into many requests and hit the 5/min limit — bound the range, or
pass `limit` high enough to fit in one page.

> On Basic, `to` must be **on or before the previous trading day**. Including today returns
> `Your plan doesn't include this data timeframe`.

### 5.4 Full Market Snapshot — *Starter+, 403 on free*

The endpoint the archived design was built on. Documented here so the 403 is recognisable.

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,TSLA,GOOG
```

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["ALAB", "MRVL", "MU"],
)
for snap in snapshots:
    print(snap.ticker, snap.last_trade.price, snap.todays_change_percent)
```

Python model (`TickerSnapshot`) — note these names, the archived doc got them wrong:

| Field | Type | Notes |
|---|---|---|
| `ticker` | `str` | |
| `day` | `Agg` | Today's bar: `.open .high .low .close .volume .vwap` |
| `prev_day` | `Agg` | Previous session's bar |
| `min` | `MinuteSnapshot` | Most recent minute bar |
| `last_trade` | `LastTrade` | `.price .size .exchange .timestamp` — plan-gated |
| `last_quote` | `LastQuote` | Plan-gated |
| `todays_change` | `float` | |
| `todays_change_percent` | `float` | |
| `updated` | `int` | Nanosecond timestamp |

There is **no** `day.previous_close` and **no** `day.change_percent`. The previous close is
`prev_day.close`; the change percent is `todays_change_percent` on the snapshot root. JSON field
names are camelCase (`prevDay`, `lastTrade`, `todaysChangePerc`); the Python client snake_cases them.

### 5.5 Unified Snapshot (v3) — *Starter+, 403 on free*

Better shaped than v2 for FinAlly if the key is ever upgraded: it exposes the session open and
previous close as named fields, which is exactly what PLAN.md §6's `open_price` anchor needs.

```
GET /v3/snapshot?ticker.any_of=AAPL,GOOGL,MSFT&limit=250
```

| Param | Description |
|---|---|
| `ticker.any_of` | Comma-separated tickers, **max 250** |
| `type` | `stocks`, `options`, `fx`, `crypto`, `indices` |
| `limit` | Default 10 — **must be raised**, or you silently get 10 tickers |
| `order`, `sort`, `ticker.gt/gte/lt/lte` | Filtering and ordering |

```python
snapshots = client.list_universal_snapshots(
    type="stocks",
    ticker_any_of=["ALAB", "MRVL", "MU"],
    limit=250,
)
for s in snapshots:
    print(s.ticker, s.session.close, s.session.open, s.session.previous_close,
          s.session.change_percent, s.market_status)
```

`UniversalSnapshotSession` fields, verbatim from the client source:

```
price, change, change_percent, early_trading_change, early_trading_change_percent,
regular_trading_change, regular_trading_change_percent, late_trading_change,
late_trading_change_percent, open, close, high, low, previous_close, volume, vwap,
last_updated, fractional_volume
```

Also on `UniversalSnapshot`: `market_status` (`open` / `closed` / `early_trading` /
`late_trading`), plus per-result `error` and `message` fields — **a bad ticker fails inside the
results array, not as an HTTP error.** Check `s.error` before reading `s.session`.

> ⚠️ `session.change_percent` is measured against **`previous_close`**, not `session.open`. PLAN.md
> §6 anchors daily change % to `open_price`. Pick one and be consistent — the two disagree by the
> overnight gap, which for these names is routinely 2-5%. Recommendation: store
> `open_price = session.previous_close` so FinAlly's percentage matches what every finance site
> shows, and rename the cache field's *meaning* in a comment rather than inventing a third number.

### 5.6 Market Status

Free on all plans and real-time. Useful to decide whether polling is worth doing at all.

```
GET /v1/marketstatus/now
```

```python
status = client.get_market_status()
print(status.market)        # "open" | "closed" | "extended-hours"
print(status.exchanges.nasdaq, status.exchanges.nyse)
print(status.early_hours, status.after_hours, status.server_time)
```

Response fields: `market`, `serverTime`, `earlyHours`, `afterHours`,
`exchanges{nasdaq,nyse,otc}`, `currencies{crypto,fx}`, `indicesGroups{...}`.

---

## 6. Error handling

| Status | Meaning | FinAlly response |
|---|---|---|
| 200 + `"status":"OK"` | Success | Normal path |
| 200 + empty `results` | Non-trading day, or no data | Step back one calendar day, retry (§8) |
| **401** | Bad/missing key | Fail fast at startup with a clear message |
| **403** `NOT_AUTHORIZED` | Plan doesn't cover this endpoint | **Downgrade mode, don't crash** (§4) |
| **429** | Rate limit (free: 5/min) | Back off; widen the poll interval |
| 5xx | Server error | Client retries 3× automatically; then keep the last cached price |

A 403 is a **permanent** condition for the life of the key — never retry it in a loop. Probe once at
startup, pick a mode, and log the decision.

```python
from urllib3.exceptions import HTTPError
from massive.exceptions import BadResponse

try:
    snapshots = client.get_snapshot_all(market_type="stocks", tickers=tickers)
except BadResponse as exc:
    if "NOT_AUTHORIZED" in str(exc):
        log.warning("Massive key lacks snapshot entitlement; falling back to anchored mode")
        return None
    raise
```

---

## 7. Async usage

`RESTClient` is **synchronous** (urllib3). Calling it directly from a FastAPI coroutine blocks the
event loop — which, under PLAN.md §3's single-worker model, stalls every SSE connection at once.
Always offload:

```python
import asyncio

bars = await asyncio.to_thread(
    client.get_grouped_daily_aggs,
    date="2026-08-07",
    adjusted=True,
)
```

---

## 8. FinAlly usage patterns

### Picking the most recent trading day

`grouped` needs a real trading day; today may be a weekend, a holiday, or (on Basic) simply not yet
available. Walk backwards until a call returns rows:

```python
from datetime import date, timedelta

async def latest_session_bars(client, max_lookback: int = 7):
    """Return (session_date, {ticker: bar}) for the most recent day with data."""
    day = date.today()
    for _ in range(max_lookback):
        bars = await asyncio.to_thread(
            client.get_grouped_daily_aggs, date=day.isoformat(), adjusted=True
        )
        if bars:
            return day, {b.ticker: b for b in bars}
        day -= timedelta(days=1)
    raise RuntimeError(f"No Massive session data in the last {max_lookback} days")
```

Bounding the loop matters: on a free key today's date always returns empty, so an unbounded walk
burns the 5/min budget in seconds.

### Startup capability probe

One cheap call decides the mode for the whole process:

```python
async def detect_mode(client) -> str:
    """Return "live" (snapshots entitled) or "anchored" (EOD aggregates only)."""
    try:
        await asyncio.to_thread(
            client.get_snapshot_all, market_type="stocks", tickers=["AAPL"]
        )
        return "live"
    except Exception as exc:
        if "NOT_AUTHORIZED" in str(exc) or "403" in str(exc):
            return "anchored"
        raise
```

### Mapping API fields onto the PLAN.md §6 cache entry

| Cache field | Live mode (Starter+) | Anchored mode (Basic) |
|---|---|---|
| `price` | `last_trade.price` (or `session.close`) | Simulated from the anchor |
| `prev_price` | Previous poll's `price` | Previous tick's `price` |
| `open_price` | `session.previous_close` / `prev_day.close` | Grouped bar `c` for the last session |
| `ts` | `updated` (ns) or `last_trade.timestamp` (ms) → seconds | Wall clock at tick |
| `history` | Appended per poll | Appended per tick |

**Timestamp units are inconsistent across this API** — aggregates use **milliseconds** (`t`), v2
snapshot `updated` uses **nanoseconds**, and v3 `last_updated`/`sip_timestamp` use **nanoseconds**.
Normalise to float Unix seconds at the boundary; don't let raw provider timestamps past the adapter.

---

## 9. Verification log

Probes run 2026-08-10 against `MASSIVE_API_KEY` from the project root `.env`:

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,MU,AMD  -> 403 NOT_AUTHORIZED
GET /v3/snapshot?ticker.any_of=AAPL,MU,AMD&limit=250                   -> 403 NOT_AUTHORIZED
GET /v2/last/trade/AAPL                                                -> 403 NOT_AUTHORIZED
GET /v2/aggs/ticker/AAPL/prev?adjusted=true                            -> 200 (close 308.26)
GET /v2/aggs/grouped/locale/us/market/stocks/2026-08-07?adjusted=true  -> 200 (12,414 tickers)
GET /v2/aggs/ticker/MU/range/1/day/2026-07-01/2026-08-07               -> 200 (10 bars)
```

**Conclusion: this project's key is on the Basic (free) tier.** FinAlly must therefore treat
"`MASSIVE_API_KEY` is set" as meaning *real anchor prices are available*, **not** *live prices are
available*. PLAN.md §5's binary "key set → real data, key unset → simulator" is too coarse; the
resolution is the three-mode design in [MARKET_INTERFACE.md](MARKET_INTERFACE.md).

---

## Sources

- [Massive API docs](https://massive.com/docs)
- [Stocks REST overview](https://massive.com/docs/rest/stocks/overview)
- [Full Market Snapshot](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot)
- [Unified Snapshot](https://massive.com/docs/rest/stocks/snapshots/unified-snapshot)
- [Daily Market Summary](https://massive.com/docs/rest/stocks/aggregates/daily-market-summary)
- [Previous Day Bar](https://massive.com/docs/rest/stocks/aggregates/previous-day-bar)
- [Custom Bars](https://massive.com/docs/rest/stocks/aggregates/custom-bars)
- [REST request limits](https://massive.com/knowledge-base/article/what-is-the-request-limit-for-massives-restful-apis)
- [Pricing](https://massive.com/pricing)
- [Polygon is now Massive](https://massive.com/blog/polygon-is-now-massive)
- [client-python on GitHub](https://github.com/massive-com/client-python) · [`massive` on PyPI](https://pypi.org/project/massive/)
