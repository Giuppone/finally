# Review: working-tree changes since `HEAD`

## Findings

### P1 — The analytics drawer reports a partially invested portfolio as fully invested

[`AnalyticsPanel.tsx`](../frontend/components/AnalyticsPanel.tsx) normalizes the selected
position weights to 1 before calling `postRisk`. Since that request omits `cash_weight`,
[`post_risk`](../backend/app/analytics/routes.py) derives it as `1 - sum(weights)`, which is
zero. A book with $2,000 invested and $8,000 cash is therefore displayed as a fully invested
two-stock book, materially overstating volatility and VaR and misreporting expected return and
Sharpe. Keep weights as fractions of total portfolio value and send residual cash, or use the
live-portfolio API path for the initial selection. Add a cash-heavy UI/API regression test.

### P1 — Session import accepts non-finite floats and persists a corrupted account

[`SessionDocument`](../backend/app/routes.py) and [`SessionPosition`](../backend/app/routes.py)
use range constraints but do not reject `Infinity`. Pydantic accepts positive infinity for
`cash_balance`, `quantity`, and `avg_cost`; [`import_session`](../backend/app/db.py) then writes
those values to SQLite. Subsequent valuation/snapshot responses contain non-finite values and may
fail serialization, while the invalid state remains in the database. Explicitly require finite
numbers before acquiring the trade lock, and cover each numeric field with route tests.

### P1 — Session exports can combine values from different portfolio states

[`get_session`](../backend/app/routes.py) executes several autocommit reads (`value_portfolio`,
`cash_balance`, `positions`, and `watchlist`) without a read transaction or `trade_lock`. In WAL
mode each statement can observe a separate snapshot, so a trade committed between reads can yield,
for example, pre-trade cash and post-trade positions. Restoring that document no longer restores an
account state and can create or destroy value. Read the entire export in one SQLite read transaction
or serialize export with the trade lock.

### P2 — Rebalance charts compare weights on different bases

[`build_plan`](../backend/app/analytics/rebalance.py) emits `current_weight` as a share of total
portfolio value, including cash, but emits `target_weight` as a share of the invested sleeve.
[`RebalancePreview`](../frontend/components/RebalancePreview.tsx) renders them as “Current vs target
weight.” With the default retained-cash behavior, an already correctly allocated 20%-invested book
looks like 20% current bars versus 100% target bars. Use one denominator for both values (usually the
invested sleeve), or label and scale targets as total-portfolio weights.

### P2 — Explicit rebalance weights are silently ignored

The advertised `holdings` shape for [`POST /api/analytics/rebalance`](PORTFOLIO_ANALYTICS.md)
includes optional weights, documented as omittable to mean “current.” However,
[`post_rebalance`](../backend/app/analytics/routes.py) discards the weights returned by `_weights`
and builds the before-state/current values solely from the live account. An API caller supplying a
90/10 allocation thus receives a plan for a different, live allocation without warning. Either make
`holdings` a ticker-only universe selector, or use supplied weights consistently when calculating
the preview and plan.

## Verification

- `cd backend && .\\.venv\\Scripts\\python.exe -m pytest -q` — 299 passed.
- Confirmed directly that `SessionDocument` currently accepts `Infinity` for `cash_balance`,
  position `quantity`, and `avg_cost`.
