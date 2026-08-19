# Portfolio evolution — reconstructing a real CEDEAR ledger as a daily USD curve

**Status:** implemented 2026-08-18.
**Purpose:** show what the portfolio was actually worth on every day since January, instead of
only since the page loaded. Everything before this feature covered minutes: `portfolio_snapshots`
starts empty on a fresh volume and samples every 30s, and the price ring buffer holds ~8 minutes.

`import_broker` (`REBALANCE_TEST_HARNESS.md` §6) already read the user's real Argentine brokerage
account, but deliberately threw away everything except the **proportions**, because CEDEARs are
fractional claims on a US share at a per-stock ratio, priced in pesos, and a nine-figure peso book is
not a $10,000 one.

A **dated** transaction ledger changes what is possible. With dates, the CEDEAR ratios stop being
unknown and become *measurable*, and the whole real book can be rebuilt day by day and valued at
real US closes.

---

## 1. Deliverables

| Path | Role |
|---|---|
| `backend/app/history/ledger.py` | parsing the export, and the canonical document |
| `backend/app/history/fx.py` | the ARS/USD rate, measured from the ledger's own bond rows |
| `backend/app/history/bars.py` | daily closes, read from `backend/calibration/bars.json` |
| `backend/app/history/reconstruct.py` | ratios, the opening book, the carry bucket, the curve |
| `backend/app/history/session.py` | the curve, shaped as a `POST /api/session` document |
| `backend/app/history/routes.py` | `GET /api/history/{portfolio,prices,prices/{t},session}` |
| `backend/calibration/ledger.json` | **generated, GITIGNORED** — personal data; baked into locally-built images only |
| `backend/scripts/portfolio_tool.py` | `ledger` and `load_history` subcommands |
| `scripts/import_broker_with_dates.{sh,ps1}` | wrapper → `portfolio_tool.py ledger` |
| `scripts/load_history.{sh,ps1}` | wrapper → `portfolio_tool.py load_history` |
| `frontend/components/RangeSelector.tsx` | the shared `LIVE 1M 3M 6M YTD MAX` strip and `$`/`%` toggle |
| `frontend/components/PnlChart.tsx` | live snapshots **or** the daily curve |
| `frontend/components/MainChart.tsx` | live ring buffer **or** daily closes for the selected name |
| `backend/tests/test_history_{ledger,reconstruct,routes}.py` | 54 tests, no network |

---

## 2. The input

Tab-separated, one row per executed transaction:

```
Fecha       Tipo             Ticker  Cantidad  Moneda  Precio    Neto
2026-08-13  Compra           MP      820       ARS     8610.0    7081130.82
2026-07-31  Venta            AMZN    1341      USD     1.93      2581.43
2026-07-07  Compra           AL30    2717      USD     0.64      1749.48
2026-07-07  Venta            AL30    2717      ARS     982.8     2660519.39
2026-06-15  Dividendos Cash  META    204       USD     0.0       2.89
```

109 rows on the real file: 88 buys and sells, 21 income rows. `Dividendos Cash`, `Renta` and
`Amortizacion` are **ignored** — the instruction was explicit — but they are counted and reported,
because a silent drop and a deliberate one look identical in the output.

An **unrecognised** `Tipo` is fatal, unlike a malformed row. There is no way to tell whether an
unknown transaction type moved a position, and dropping a whole category misstates the book with
no visible symptom anywhere.

---

## 3. Four things the file does not say, and where each comes from

### 3.1 The exchange rate — from the bond rows

Look at the two AL30 rows above: same date, same ticker, same quantity, opposite sides, different
currencies. That is not two trades. It is one currency conversion — buy a sovereign bond in
dollars, sell the same bond in pesos the same day — and the ratio of the two `Neto` values is the
**exact** rate that trade executed at, spread included.

Eight of them on the real ledger, monotone as the peso depreciates across the window —
roughly 1420 → 1520 ARS/USD. (The dated table lives in the local artifact, not here: the
observations are the user's actual conversion days.)

Linear interpolation between, **flat outside**. Flat rather than extrapolated on purpose: the peso
only ever depreciates, so a linear extension off the last two points would keep depreciating
forever and quietly inflate every dollar figure past the end of the data.

All four matching conditions are load-bearing — drop any one and real trades start matching. Two
same-day sales of the same size in the same currency are two sales. `test_history_ledger.py`
parametrises all four near-misses.

The matched rows are then **netted out of the position walk**. They moved currency, not holdings;
counting them as a real buy and a real sell nets to zero units but double-counts the cash.

### 3.2 The CEDEAR ratios — from the trades

A CEDEAR priced in USD is worth `us_close / ratio`, so `ratio = us_close / cedear_price_usd`, and
a peso price becomes a dollar price through §3.1. Take the **median** across every trade in a
ticker:

```
MU ~4.85   AMD ~9.6   MSFT ~29.3   NVDA ~23.2   MRVL ~13.5   AMZN ~140
ASML ~138  SNDK ~162  LRCX ~53.8   SMH ~48.4    TSM ~8.6     INTC ~4.8
```

These agree with the published BYMA conversion ratios — they are public constants, recovered
here from the trades rather than looked up. Per-ticker spreads on the real file come out under
5% for every name with more than two observations.

Median, not mean: a single fill at an intraday extreme would drag a mean noticeably, and one wrong
ratio silently rescales that entire position for the whole curve. The residual spread is
fill-versus-close noise, and it averages out.

**The fallback matters.** A ticker held across the window but never *traded* inside it has no
observation at all — GOOGL, on the real file. Its ratio comes from the current-holdings export
instead: `ratio = us_close(snapshot day) / (ars_price_today / fx(snapshot day))`. Without this it
drops out of the priced set and roughly 4% of the book vanishes from the curve with no error
anywhere; the only symptom is a total that is slightly too small.

### 3.3 The opening book — back-solved

The export carries no opening balance, so a name sold-but-never-bought inside the window goes
negative. `opening = current_holdings − net_ledger_flow`, with current holdings parsed from
`suggested/sugested.txt` by the existing `parse_broker`.

On the real pair of files **every opening solves non-negative**, and more than half the tickers
land on *exactly* zero, meaning they were built entirely inside the window. That is the strongest available evidence that
the two files describe the same account.

A negative result is therefore a genuine inconsistency between two files, not a rounding artefact,
so it is reported rather than clamped silently. Clamping produces a plausible-looking curve that is
wrong.

### 3.4 The starting cash — the least that avoids implied borrowing

The ledger records trades, not deposits, so the running balance starts at zero and goes negative
the first time a purchase precedes the sale that funded it. Left alone
that reads as margin the account never had, and `SessionDocument.cash_balance` is `ge=0`, so the
loader would 422 on it.

`opening_cash = max(0, −min running cash)`, credited at day zero rather than on the day it is first
needed — the same no-artificial-step discipline the carry bucket follows.

---

## 4. Instruments with no US listing: carried at cost

Argentine bonds (AL30, GD35, GD38, AE38, S29Y6) and locally-listed equities (GGAL, PAMP, YPFD,
TGNO4) have no US daily closes and never will. A ticker that *is* US-listed but merely absent from
the bars cache lands here too, which is a fixable state rather than a permanent one - see §11. They are not dropped — they were a real, material part of the
book on day one.

Rule: **the opening quantity is valued at its first transacted USD price, and later buys and sells
just move value between the carry bucket and USD cash.** The total is therefore continuous across
the January liquidation, instead of stepping up by $21,165 on 2026-01-23 as if money had appeared.

`test_history_reconstruct.py` asserts that invariant directly, in both directions — selling a
carried instrument and buying one.

**The exclusion rule is "has US daily closes", not "normalises".** GGAL, PAMP and YPFD pass every
ticker regex in this project. If they reached `positions` they would land in `db.tracked_tickers`
(`watchlist ∪ positions`), then in `service.sync_tracked`, and the simulator would invent a GBM
price path for a stock that has no US listing.

---

## 5. Two bugs the reconciliation caught

The end-state check — do the walked positions land exactly where the holdings export says? — is the
single most valuable thing in the feature. Every step feeds the terminal position, so when the rate,
the ratios, the openings and the calendar are all right it comes out exact, and when any one is
wrong this is usually the only place it shows. Both of these were found that way and nowhere else.

1. **A ledger date that is not a US trading day silently dropped its flows.** 2026-01-19 was Martin
   Luther King Jr Day: the US market was shut, so it is not a trading day, but the Argentine market
   was open and the ledger has two sales on it. Iterating over trading days alone silently dropped both
   of that day's sales — and the reconciliation failed on exactly those two tickers.
   The calendar must be **trading days ∪ ledger dates**.

2. **A held-but-never-traded ticker had no derivable ratio** — §3.2 above.

Both are named regression tests.

---

## 6. Why the app reads a generated artifact, not the raw file

`.dockerignore` and the Dockerfile copy only `backend/` and the frontend build into the image.
`example/` and `suggested/` are **not** in it, so the ledger does not exist at runtime and the app
must not parse it live.

So `import_broker_with_dates` reduces the raw export to a committed document at
`backend/calibration/ledger.json` — exactly the relationship `calibrate_market.py` has with
`app/market/seeds.py`, and in the same directory as `bars.json` for the same reason.

The document carries **inputs, not conclusions**: the rows, the back-solved opening quantities, and
the holdings snapshot. The rate, the ratios and the curve are all recomputed at runtime, because
each is a function of `bars.json` — which grows. Freezing them would pin the curve's right edge to
whatever day the document was generated, which is the opposite of the point.

### Bars stay in `bars.json`, not a `daily_bars` table

`/app/db` is a named volume. Seed-on-first-run means an image rebuild shipping fresher bars has no
effect, because the volume already has rows — and `init_db` is a single `executescript` with no
migration framework to reconcile that with. These bars are also immutable reference data versioned
with the code, not user state; putting them in the user-state store is a category error, and
`POST /api/portfolio/reset` would have to learn to leave one table alone. It is 68 KB.

`bars.py` and `ledger.py` both resolve their path with `Path(__file__).parents[2]`, which is
correct in **both** layouts: `backend/calibration/` in a checkout, `/app/calibration/` in the image,
because the Dockerfile flattens `backend/` onto `/app`. Same trick as `db.db_path()`.

### The curve is computed lazily and memoised on file identity

Not at startup: the current `lifespan` is deliberate about what may abort a boot (`verify_config()`
is the only fail-fast and is documented as such), and a missing ledger must degrade one panel, not
the app. It runs in `asyncio.to_thread` for the reason `db.run` exists — every SSE connection shares
that loop — behind a double-checked lock so two first requests do not both compute it.

In the image both files are baked in, so it computes once per process. In a checkout, regenerating
either file is picked up on the next request with no restart.

---

## 7. `app/history/__init__.py` does not export its router

`app/analytics/__init__.py` exports `router`. This package deliberately does not, and `main.py`
reaches for it explicitly:

```python
from .history.routes import router as history_router
```

Because `pyproject.toml` has `packages = ["app"]`, `backend/scripts/` is **not** in the wheel, so
`app` importing `scripts` would resolve only by cwd accident. The dependency therefore runs the
other way: `portfolio_tool.py` imports `app.history`, under whatever bare `python3` the host
happens to have — `lib_portfolio_tool.sh` picks a plain interpreter long before it considers
`uv run`. Every module in the package is stdlib-only for that call site. Re-exporting the router
would drag FastAPI into that import and fail with `ModuleNotFoundError` on a machine that has never
installed the backend.

### The other trap in the same area

`parse_amount` reads **Argentine** grouping, where `305.650` is three hundred five thousand six
hundred fifty. The ledger is plain dot-decimal, where `8610.0` is eight thousand six hundred and
ten. The two strings look identical and are a thousand apart. `ledger.py` uses `float()` and does
**not** reuse `parse_amount`; a test asserts the two disagree, so nobody unifies them.

---

## 8. Endpoints

New siblings under `/api/history`, not parameters on the existing routes.

| Method | Path | Description |
|---|---|---|
| GET | `/api/history/portfolio?range=` | the daily USD curve, with `total_value` **and** `return_pct` |
| GET | `/api/history/prices?tickers=&range=` | bulk daily closes; a stale symbol yields `[]`, never a failed batch |
| GET | `/api/history/prices/{ticker}?range=` | one ticker's daily closes; 404 on an unknown symbol |
| GET | `/api/history/session` | the reconstructed book as a `POST /api/session` document |

`range` is `1m | 3m | 6m | ytd | max`. **No `1y`** — the bars cache starts 2025-12-07, so a year
option would return eight months and call it a year.

Why not extend what already exists:

- `/api/portfolio/history` reads `portfolio_snapshots`: the *simulated* $10,000 account, 30-second
  granularity, mutated by every trade, denominated against `STARTING_CASH`. This serves the *real*
  account, daily, immutable, six figures. One path returning both makes `total_value` mean different
  things depending on a query parameter, and the frontend branches on its own input anyway.
- `/api/prices/{ticker}/history` is `Depends(get_service)` and **404s when the ticker is not
  tracked**. Daily closes exist for anything in the bars cache, tracked or not, so an
  `interval=1d` switch would force either loosening that 404 — a behaviour change to the route the
  main chart depends on — or refusing daily data for untracked names.

**`return_pct` is rebased server-side to the first point of the *filtered* window.** A client
rebasing `max` data to draw `3m` shows the wrong number, and one refetching per toggle flickers; so
the `$`/`%` switch is a field swap, not a round trip.

**Dates are ISO calendar dates, with an epoch-ms `ts` alongside.** A daily close has no time of day,
and stamping one `T00:00:00Z` shifts it a day for any viewer west of UTC — but market data in this
project is epoch-ms by convention, and carrying both lets one chart component read this and the live
ring buffer through the same accessor. The frontend formatters pass `timeZone: "UTC"` for the same
reason.

### Degrading

A missing ledger returns **200 with `available: false`**, not 404: the frontend fetches this on
every page load, so a stock deployment would log one each time, and the panel needs to render an
explanation rather than an error. `/api/history/session` is the exception and 404s — that caller
asked for one specific thing to load and there is none.

A bars cache that is missing, unreadable, or does not cover the ledger also reports unavailable. A
curve *could* be drawn from the carry bucket alone, and it would be a near-flat line of transacted
costs that looks like a portfolio and tracks nothing.

**The right edge is as fresh as `bars.json`,** which is why every response carries
`meta.bars_through` and the chart footer says "through 2026-08-14". Extending it is
`calibrate_market.py --yes`. The live $10,000 paper book is deliberately **not** spliced onto the
end: it is a different account unless `load_history` has been run.

---

## 9. `load_history`

Fetches `GET /api/history/session` and restores it through `POST /api/session`.

**It fetches rather than recomputes**, so the reconstruction math lives in one place instead of
forking into a stdlib CLI copy that drifts from the curve the chart is drawing.

**It posts to `/api/session`** because that is the only endpoint that sets an exact quantity *and*
an exact average cost, in one transaction, under the same `trade_lock()` as a trade. Replaying the
ledger as market buys fills every leg at today's price and rewrites every cost basis with it —
`REBALANCE_TEST_HARNESS.md` §6 records that as the reason these endpoints exist.

| Field | Value |
|---|---|
| `quantity` | walked share equivalents (`cedear_qty / ratio`), which reconcile 24/24 |
| `avg_cost` | real USD cost basis; averaged on buy, reduced pro rata on sell, as `portfolio._apply` does it (the ledger has no lot ids, so FIFO is not available) |
| `cash_balance` | the reconstruction's terminal USD cash — non-negative by §3.4 |
| `watchlist` | the priced tickers only |

Three validators on the receiving side shape what may be emitted, and none should be relaxed:
`cash_balance ge=0` (the sim account cannot buy on margin), `quantity gt=0` (a zero row is the
phantom position a full sell deletes), and the letters-only ticker normalisation (which is why
digit-bearing symbols must never reach the document).

`--dry-run` writes nothing; without `--yes` it prompts, because it replaces positions, trades and
P&L history. `ledger` is added to the docker-exec refusal list in both `lib_portfolio_tool` files —
it writes a file under `backend/calibration/`, and the container's filesystem is not the host's.
`load_history` is pure HTTP and stays allowed.

---

## 10. Frontend

One `LIVE 1M 3M 6M YTD MAX` strip on the P&L chart and another on the main chart, plus a `$`/`%`
toggle on the former. `LIVE` keeps every existing behaviour exactly — snapshots and the SSE ring
buffer — and the longer ranges draw daily closes.

The P&L chart **defaults to `MAX`** when a ledger is available, so the evolution is what the page
opens on.

**The daily ticker chart marks the user's own trades.** The price series legitimately covers the
whole bars window, but that alone caused a real misreading: a position opened days earlier drew
months of prior price history and read as a long-held one. `GET /api/history/prices/{ticker}` therefore carries
`trades` (the user's buys and sells in share-equivalent terms, at their actual converted fill
price — not that day's close) and `held_since`; the chart renders B/S markers with a tooltip and a
"held since" caption in the header. A ticker the ledger never touched gets an empty list and no
caption.

Notes worth keeping:

- Each chart renders its two modes as **separate components**, not one chart with a union `data`
  prop. Recharts infers its point type from that prop, so `SnapshotPoint[] | CurvePoint[]` does not
  type-check — and the modes already differ in axis, tick formatter, tooltip, dataKey and reference
  line, so sharing the element saved nothing.
- A range the backend cannot answer is **not offered**. Without a ledger the P&L strip shows `LIVE`
  alone; a ticker with no bars shows `LIVE` alone. Offering a button that returns an empty panel
  reads as breakage.
- Break-even is `STARTING_CASH` on the live view and the **window's own first close** on the curve.
  Measuring a six-figure real account against the paper book's $10,000 means nothing.
- Daily data is fetched once per `(key, range)` into a ref and kept **out of the 15s refresh
  interval** — it derives from an immutable artifact, so re-fetching it every 15 seconds is waste.
- `lib/format.ts` gained `shortDate` / `longDate`. `clockTime` and `timeOfDay` print time of day
  only, so a seven-month axis would have rendered every tick identically.

---

## 11. The result, on the real files

```
~90 trades, ~20 income rows ignored
16 rows netted as currency conversions -> 8 exact ARS/USD observations
26 tickers priced (25 ratios from trades, 1 from the holdings snapshot), 9 carried at cost

reconciled: all 26 priced tickers end exactly where the holdings file says they should
```

The reconstructed final value lands within ~2% of the real book converted at the last observed
rate, with the residual explained by the carry-at-cost names held flat and FX drift past the
last observation. The exact figures are in the local `ledger.json` and the importer's output —
deliberately not in this document, which is committed to a public repository.

QCOM and RGTI are US-listed and were only carried because they were absent from the bars cache;
`calibrate_market.py --tickers QCOM,RGTI --yes` moved their opening carry into properly priced
positions. That run also recalibrated the whole basket, as `resolve_tickers()` always does — the
common window went 172 -> 171 days and every sigma moved in the third decimal place. Expect that,
and review the `seeds.py` diff before committing it.
