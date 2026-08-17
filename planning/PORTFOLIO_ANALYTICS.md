# Portfolio Analytics — Risk / Expected Return and Rebalance Suggestion

**Status:** **implemented 2026-08-16**, phases 1-5. Only phase 6 (the realised-returns estimator)
remains open, and it is optional - see §10. This doc now describes what exists; the reasoning is kept because
it is the part worth re-reading.
**Scope:** two new buttons in the terminal, the endpoints behind them, and the risk model they share.
**Companion doc:** `REBALANCE_TEST_HARNESS.md` — the seed scripts that put the portfolio into a known
lopsided state so these buttons have something to say.

---

## 1. What the user gets

Two buttons, both acting on a **selection** (tickers + weights), both read-only by default:

| Button | Label | Answers |
|---|---|---|
| A | **Risk & Return** | "If I hold *these* names at *these* weights, what volatility, expected return, and Sharpe am I taking, and which position is responsible for the risk?" |
| B | **Suggest Rebalance** | "Given the same names, what weights would I rather hold — and exactly which trades get me there?" |

Button B **suggests**. It returns a trade list and a before/after comparison; nothing executes until the
user presses **Apply** (§8), which is a separate, explicit action.

### Where the selection comes from

The panel opens pre-filled with the **current portfolio weights** (`/api/portfolio` already returns
`weight` per position). The user can then:

- toggle tickers on/off — from positions *and* from the watchlist, so you can ask "what if I added SLV?"
- edit weights inline; the panel normalises to 100% and shows the residual as cash

This is why the endpoints take an explicit `holdings` array rather than reading the DB: the whole point
of button A is answering hypotheticals. Passing the current portfolio is just the default case.

---

## 2. The risk model, and why it is the honest one here

This is the decision the rest of the feature hangs off.

**Σ and μ come from `app/market/seeds.py` — the same table the price simulator draws from.**

```
Σ_ij = ρ_ij · σ_i · σ_j          ρ from market.simulator.correlation_matrix()
Σ_ii = σ_i²                       σ, μ from seeds.TICKER_PARAMS
unknown ticker → seeds.DEFAULT_PARAMS (σ=0.45, μ=0.05), unknown pair → DEFAULT_RHO (0.35)
```

Reuse `market.simulator.correlation_matrix(tickers)` **by import, not by copy**. It already handles the
sector-block lookup, the `"*"` wildcards, and the `_ridge()` PSD repair that a bad `SECTOR_RHO` edit
would otherwise turn into a crash. A second copy of that logic is a second thing to keep in sync, and a
divergence would mean the risk panel describes a different world than the one generating the prices.

Why this is not a shortcut:

- In `SIMULATED` and `ANCHORED` — the two modes this project actually runs in — prices *are* GBM paths
  with exactly these σ and these correlations. The analytics is therefore **exactly right about the
  world the user is trading in**, not an estimate of it. That is a stronger guarantee than fitting a
  covariance to 8 minutes of 500ms ticks, which is what the ring buffer could offer.
- The table was itself calibrated from real Massive daily bars (2025-12-01 → 2026-08-07, see the header
  of `seeds.py` and `MARKET_SIMULATOR.md` §9), so the numbers are not invented.

### The μ caveat — state it in the UI, do not hide it

`TICKER_PARAMS.mu` is **deliberately damped** — roughly 10% of realised drift, capped at 0.20 — because
undamped drift made every position profitable and the heatmap's red/green encoding meaningless
(`MARKET_SIMULATOR.md` §6). It is the true drift of the simulated process, but it is *not* a forecast of
the real stock, and the cap compresses the ranking at the top (MU's realised 2.30 and ALAB's are both
pinned near the cap).

Consequences, both handled by design rather than by disclaimer:

1. The response carries `"expected_return_basis": "simulator-calibrated (damped drift)"` and the panel
   renders it next to the number. No unlabelled forecast.
2. **The default rebalance objectives do not use μ at all.** Min-variance, risk-parity and equal-weight
   depend only on Σ. Max-Sharpe is offered but is not the default, and its result carries the same
   basis label. An optimiser fed a capped μ produces confidently wrong tilts; one fed only Σ does not.

### Phase 2 option — realised estimates from daily bars

A Basic Massive key is aggregates-only, which means `get_aggs` daily closes **are** available on this
repo's key even though snapshots are not. An `estimator=realized` query parameter can fetch ~180 daily
closes per ticker, compute log-return μ/σ and the full sample correlation, and cache it per session date.
Worth doing under `LIVE` (where the seeds table no longer describes the price process); optional
elsewhere. Keep it behind the parameter — it costs N calls against a 5-req/min sliding window, and the
default path must never depend on the network.

### Constants

```python
RISK_FREE_RATE = 0.04        # annual; used by Sharpe and as the return on the cash sleeve
TRADING_DAYS   = 252
VAR_Z_95       = 1.645
```

Document `RISK_FREE_RATE` as an assumption in the response (`"risk_free_rate": 0.04`) so a reader can
re-derive the Sharpe. Cash is modelled as a risk-free asset: σ=0, μ=`RISK_FREE_RATE`, zero covariance
with everything. That matters — a 60%-cash portfolio genuinely is lower-vol, and pretending weights must
sum to 1 over risky assets alone would overstate its risk.

---

## 3. Math

Let `w` be the risky weight vector (`Σwᵢ ≤ 1`), `w_c = 1 − Σwᵢ` the cash weight.

| Quantity | Formula | Note |
|---|---|---|
| Expected return | `μ_p = Σ wᵢμᵢ + w_c·r_f` | annualised |
| Volatility | `σ_p = √(wᵀΣw)` | annualised; cash contributes nothing |
| Sharpe | `(μ_p − r_f) / σ_p` | `null` when `σ_p = 0` (all cash), never `inf` |
| Marginal risk | `MCRᵢ = (Σw)ᵢ / σ_p` | |
| Risk contribution | `RCᵢ = wᵢ · MCRᵢ` | **`Σ RCᵢ = σ_p` exactly — assert this in tests** |
| Risk share | `RCᵢ / σ_p` | what the bar chart plots; sums to 100% |
| Diversification ratio | `(Σ wᵢσᵢ) / σ_p` | 1.0 = no diversification benefit |
| Effective N | `1 / Σwᵢ²` | inverse HHI; concentration in one number |
| 1-day 95% VaR | `1.645 · σ_p/√252 · total_value` | parametric-normal; see below |

**VaR approximation.** GBM returns are lognormal, not normal. At a one-day horizon the difference is
immaterial and the normal parametric form is the industry-standard reporting convention, so use it — but
label the field `var_95_1d_parametric` rather than `var_95`, so nobody mistakes it for a simulated tail.

**Correlation matrix** for display is `correlation_matrix(tickers)` directly — already normalised.

Numerics: n ≤ ~30 tickers here, so **pure Python, no numpy**. The backend has no numeric dependency today
(`fastapi`, `uvicorn`, `massive`, `tzdata`, `litellm`, `pydantic`) and adding one for a 30×30 matrix-vector
product would be the largest dependency in the tree for the smallest reason. Every operation below is
O(n²) or an O(n log n) sort.

---

## 4. Optimisers

All are long-only, fully invested across the selection (`Σw = 1` over the risky sleeve, then scaled by
the invested fraction), with a per-name cap.

| `objective` | Method | Uses μ? | Why offer it |
|---|---|---|---|
| `min_variance` | projected gradient on `wᵀΣw`, `∇ = 2Σw` | no | **the default**; the one answer that is unambiguously well-posed |
| `risk_parity` | fixed point `wᵢ ← wᵢ·(σ_p/n / RCᵢ)^½`, renormalise | no | the interesting answer against an equal-*weight* portfolio (§7) |
| `max_sharpe` | projected gradient on `(μᵀw − r_f)/√(wᵀΣw)` | **yes** | offered, labelled, never the default |
| `equal_weight` | `1/n` | no | baseline; also the "undo my random portfolio" button |

**Projection.** After each gradient step, project onto `{w : Σw = 1, 0 ≤ wᵢ ≤ cap}` with the standard
sort-based capped-simplex projection (O(n log n)). Fixed 500 iterations, step `1/(2·max diag Σ)`,
tolerance 1e-9, **no randomness** — same input must give byte-identical output, because the E2E asserts
on it.

**Constraints** (all optional in the request, all with defaults):

```
max_weight        0.35   per-name cap; without it min-variance dumps everything into SLV
min_weight        0.01   weights below this are zeroed and renormalised — kills dust trades
cash_target       null   fraction of total value to leave in cash; null = keep the current cash fraction
```

`max_weight` must be ≥ `1/n` or the constraint set is empty — validate and return 400 with that
arithmetic spelled out, rather than letting the projection silently fail to converge.

**Local optima:** on the long-only simplex the max-Sharpe objective is quasi-concave, so a single start
from equal weights is sound. Start min-variance from equal weights too, and max-Sharpe from the
min-variance solution — cheap, and it makes the result deterministic.

---

## 5. From target weights to trades

```
investable      = total_value − cash_reserve          (cash_reserve from cash_target)
target_valueᵢ   = investable · wᵢ
deltaᵢ          = target_valueᵢ − current_market_valueᵢ
quantityᵢ       = round(|deltaᵢ| / priceᵢ, 4)          fractional shares are supported (§7 schema)
```

Four rules, each earning its place:

1. **Sells first, then buys.** A buy-first ordering fails on insufficient cash for exactly the trades
   that most need to happen. This is the same sequential-validation principle as PLAN.md §9.
2. **Drop `|delta| < MIN_TRADE_NOTIONAL` ($10).** Otherwise a 0.3% drift generates ten $2 trades that
   clutter the blotter and move nothing.
3. **Clamp the final buy to available cash.** Prices tick every 500ms; between the quote the plan was
   built on and the last fill, cash can come up a few cents short. Without the clamp the rebalance
   reliably 400s on its last trade. Clamp, and report the clamped quantity.
4. **Skip unpriced positions** (`priced: false` in `/api/portfolio`) — exclude them from both the risk
   math and the trade list, and return them in `warnings`. Valuing them at `avg_cost` is right for the
   portfolio panel and wrong for an optimiser, which would treat a stale name as a zero-vol asset.

---

## 6. API

Two new routes, plus one optional executor. New router `app/analytics/routes.py`, mounted under
`/api/analytics` in `main.py` alongside the existing routers.

### `POST /api/analytics/risk`

```jsonc
// request
{
  "holdings": [ {"ticker": "AMD", "weight": 0.4}, {"ticker": "SLV", "weight": 0.35} ],
  "cash_weight": 0.25          // optional; default = 1 − Σweight, clamped at 0
}
```

```jsonc
// response
{
  "expected_return": 0.118,          // annualised, decimal
  "volatility": 0.512,
  "sharpe": 0.152,                   // null when volatility == 0
  "var_95_1d_parametric": 412.77,    // currency, needs total_value; null if not supplied
  "diversification_ratio": 1.31,
  "effective_n": 2.4,
  "risk_free_rate": 0.04,
  "expected_return_basis": "simulator-calibrated (damped drift)",
  "positions": [
    {"ticker": "AMD", "weight": 0.4, "expected_return": 0.14, "volatility": 0.720,
     "marginal_risk": 0.61, "risk_contribution": 0.244, "risk_share": 0.476}
  ],
  "correlations": {"tickers": ["AMD","SLV"], "matrix": [[1.0,0.25],[0.25,1.0]]},
  "warnings": ["MU excluded: no cached price"]
}
```

### `POST /api/analytics/rebalance`

```jsonc
// request
{
  "holdings": [...],                 // same shape; weights optional — omit to mean "current"
  "objective": "min_variance",       // | risk_parity | max_sharpe | equal_weight
  "constraints": {"max_weight": 0.35, "min_weight": 0.01, "cash_target": null}
}
```

```jsonc
// response
{
  "objective": "min_variance",
  "before": { …the /risk response for current weights… },
  "after":  { …the /risk response for target weights…  },
  "targets": [{"ticker": "AMD", "current_weight": 0.40, "target_weight": 0.22,
               "current_value": 4012.0, "target_value": 2206.0, "delta_value": -1806.0}],
  "trades":  [{"ticker": "AMD", "side": "sell", "quantity": 3.7361, "price": 483.36,
               "notional": 1806.0, "clamped": false}],
  "estimated_cash_after": 500.0,
  "warnings": []
}
```

`trades` is ordered exactly as it must be executed.

### `POST /api/portfolio/rebalance` (Phase 3, optional but recommended)

Takes `{"trades": [...]}` — the list returned above — and executes it **under a single
`portfolio.trade_lock()`**, reusing `portfolio._apply` per trade and re-checking each against the
balance its predecessors left. Returns per-trade results and the final portfolio.

The alternative is looping `POST /api/portfolio/trade` from the browser. That works and reuses existing
validation, but between calls a chat-driven trade can interleave and the plan's arithmetic quietly stops
holding. One lock for the batch is the same reasoning `POST /api/portfolio/reset` already documents
(`Back_end_review.md` P1). A **partial batch is a valid outcome** — earlier fills stand, the first
failure and everything after it are reported with reasons, exactly as PLAN.md §9 specifies for LLM
trades.

### Not an SSE concern

These are request/response. Nothing here touches the price stream, the cache, or the tracked set —
except `POST /api/portfolio/rebalance`, which inherits the existing post-trade `sync_tracked` path for
free by going through `_apply`.

---

## 7. Frontend

### Files

| File | Change |
|---|---|
| `components/AnalyticsPanel.tsx` | **new** — the drawer, two tabs (Risk / Rebalance), the selection editor |
| `components/RiskScatter.tsx` | **new** — Recharts `ScatterChart`: x = volatility, y = expected return, bubble size = weight, portfolio marked distinctly |
| `components/RiskContributions.tsx` | **new** — horizontal bars, weight vs risk share side by side (the whole point in one picture) |
| `components/RebalancePreview.tsx` | **new** — current → target weight bars, then the ordered trade list, then **Apply** |
| `components/PositionsTable.tsx` | selection checkboxes + the two buttons in its header bar |
| `components/Watchlist.tsx` | a small "+ analyse" affordance so unheld tickers can enter the selection |
| `lib/api.ts`, `lib/types.ts` | `postRisk`, `postRebalance`, `postApplyRebalance` + types |
| `app/page.tsx` | drawer open state, selection state, refresh after Apply |

### Placement

A **right-edge drawer overlaying the middle column**, opened by either button, closed with Esc. The
middle column is already three tight rows (`minmax(0,1.3fr)_minmax(0,1fr)_minmax(0,0.95fr)`); adding a
fourth would squeeze the main chart below usefulness, and putting analytics in a modal loses sight of
the prices that make it interesting. A drawer keeps the watchlist ticking on the left while you read.

Both buttons open the same drawer on different tabs, so the two features share one surface and one
selection.

### Style

Existing palette (PLAN.md §2): purple `#753991` for **Apply** (it is the submit button), blue `#209dd7`
for the two open buttons, yellow `#ecad0a` for the "damped drift" basis label — it is a caveat, and
yellow is already this app's attention colour. Green/red for weight deltas, matching the heatmap so
green never means "increase" in one panel and "profit" in another.

**Read the `dataviz` skill before writing the scatter and the bar charts.** Three new chart types land in
one feature; they need to read as one system with the existing heatmap and P&L chart.

### States

- fewer than 2 selected names → the risk math still runs (σ_p of one name is just σ), but the optimiser
  returns "nothing to rebalance"; render that, do not call the endpoint
- zero positions and nothing selected → empty state pointing at the watchlist, no request fired
- request in flight → the same loading treatment the chat panel already uses
- `warnings` non-empty → a yellow strip above the results, never a silent drop

---

## 8. Apply, and what it must not do

**No auto-execution.** The LLM auto-executes trades because the demo depends on it (PLAN.md §9), and
that is a deliberate exception. A rebalance is 5–10 trades at once; firing them from a button labelled
"suggest" would violate what the button says. Apply is a second, explicit click showing the trade list.

After Apply: refresh `/api/portfolio` and `/api/portfolio/history`, take a snapshot (the existing
post-trade snapshot path does this), and re-run `/api/analytics/risk` on the new weights so the drawer
shows the realised outcome rather than the prediction.

**Optional: LLM access.** Chat could reach the same functions ("am I too concentrated?"). Deliberately
out of scope for the first pass — the structured-output schema in PLAN.md §9 has no slot for an analytics
call, and widening it is its own change with its own tests.

---

## 9. Tests

### Backend (`backend/tests/test_analytics_*.py`)

Closed-form checks, not golden files:

- **two-asset min-variance** has an analytic solution `w₁ = (σ₂² − ρσ₁σ₂)/(σ₁² + σ₂² − 2ρσ₁σ₂)` — assert
  the solver hits it to 1e-6. This is the test that catches a wrong gradient or a broken projection.
- **identical assets** (equal σ, ρ=1) → any weights give `σ_p = σ`; min-variance must not blow up
- **equal σ, equal ρ** → min-variance and risk-parity both return `1/n`
- **`Σ RCᵢ = σ_p`** exactly, for random weight vectors — the Euler-decomposition identity
- **risk-parity** produces equal `risk_share` within 1e-4
- **cap binding**: with `max_weight = 0.2` and n=4, the only feasible point is `1/n`; with `max_weight <
  1/n`, expect a 400 naming the arithmetic
- **PSD**: `correlation_matrix` output through `_cholesky` never returns `None` for the seed universe
- **trades reconstruct the target**: apply the returned trades to the current holdings at the returned
  prices → resulting weights match `targets` within the dust threshold
- **sells precede buys**; **dust filtered**; **final buy clamped** when cash is 1¢ short
- **unpriced position excluded and warned**
- **determinism**: same request twice → identical response bytes
- routes: 200 shapes, 400 on empty holdings / negative weights / unknown objective / weights summing
  above 1

### Frontend

Panel renders from a fixture response; the "damped drift" label is present whenever `expected_return` is;
Apply is disabled while a request is in flight and while `trades` is empty.

### E2E (`test/tests/`)

Uses the seed scripts from `REBALANCE_TEST_HARNESS.md`:

1. seed the equal-weight portfolio → open Risk → assert weights are equal but **risk shares are not**
   (this is the feature's whole thesis, and it is falsifiable)
2. seed the random portfolio (fixed `--seed`) → Suggest Rebalance with `min_variance` → assert
   **`after.volatility ≤ before.volatility`**. This must hold for every input; if it ever fails, the
   optimiser is wrong. It is the single most valuable assertion in this plan.
3. Apply → positions change, cash stays ≥ 0, no trade rejected
4. `risk_parity` on the random portfolio → all risk shares within 1% of each other

---

## 10. Phases

| Phase | Contents | Status |
|---|---|---|
| 1 | `app/analytics/` — estimates, risk stats, `POST /api/analytics/risk` | **done**, 31 unit + 6 route tests |
| 2 | optimisers, trade construction, `POST /api/analytics/rebalance` | **done**, incl. the closed-form and monotonicity checks |
| 3 | `POST /api/portfolio/rebalance` under one trade lock (`portfolio.execute_batch`) | **done**, 4 route tests |
| 4 | `AnalyticsPanel` + charts + selection UI | **done**, verified in Chrome against a seeded book |
| 5 | Playwright E2E | **done** — `test/tests/analytics.spec.ts`, 10 specs; whole suite 35 passing |
| 6 *(optional)* | `estimator=realized` from Massive daily aggregates | not done; still optional |

## 10b. What was built differently from this plan

| Plan said | Built | Why |
|---|---|---|
| `RiskContributions.tsx` + a separate rebalance weight chart | one `WeightBars.tsx`, used twice | Identical form — two measures per ticker as grouped horizontal bars. Two components would have been one component with two names. |
| Selection checkboxes in `PositionsTable` + a watchlist affordance | selection lives inside the drawer | Threading selection state through two dense panels to serve a third was more plumbing than it was worth, and the drawer version reads better: one list, positions and watchlist together, shared by both tabs. The terminal layout is untouched. |
| Accent yellow `#ecad0a` for the second series | `#b8860b` | `#ecad0a` sits at OKLCH L 0.786, outside the 0.48–0.67 band a categorical hue needs on a dark surface — it fails the validator and glares against `#12181f`. Same hue, snapped to a passing step. `#ecad0a` remains the UI accent everywhere else, including the "damped drift" caption in this panel. |
| — | full-exit sell uses the exact held quantity | Found by running it: truncating a full exit sold 0.4536 of 0.4537 held and left a `0.0001` phantom position in the table — the exact defect `Review.md` B11 exists to prevent. See `rebalance.py`. |

## 11. Docs to update when this lands

- `PLAN.md` §8 — the endpoint tables gain an Analytics section
- `PLAN.md` §10 — the UI element list gains the analytics drawer
- `CLAUDE.md` — current-state paragraph and the test count
