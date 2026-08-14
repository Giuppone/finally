# Working-tree review (`HEAD` plus untracked files)

## Findings

### P1 — Reset is not serialized with trade execution, so an order can survive a successful reset

[`execute_trade`](../backend/app/portfolio.py#L164) uses `trade_lock()` only around the
database mutation and its tracked-set reconciliation. [`post_reset`](../backend/app/routes.py#L116)
does neither: it calls `db.reset()` and later reconciles a tracked set that was read before
or after an overlapping trade. A trade that has already resolved its quote can therefore
commit immediately after reset, leaving a position/trade in what the reset response calls a
fresh account. In the opposite ordering, reset can apply its stale, seed-only tracked set
after the trade's reconciliation and evict the newly held ticker from market tracking.

Use the same account-operation lock for the complete reset operation (database reset,
tracked-ticker read, and `sync_tracked`), so it cannot interleave with a trade. Add a
concurrent reset/buy test that asserts the final state is one coherent outcome and that any
remaining position is tracked and priced.

### P2 — Concurrent snapshot writers can create duplicate, unchanged history points

[`_snapshot`](../backend/app/portfolio.py#L282) performs `last_snapshot()` and
`record_snapshot()` as separate autocommitted operations, without a transaction or lock.
The background `SnapshotTask` and the trade-triggered `snapshot_now()` can both observe the
same previous row (or no row), both decide the value changed, and both insert an identical
snapshot. This defeats the explicit de-duplication rule and creates extra P&L chart points
under normal timing races.

Make the read/compare/insert sequence one `BEGIN IMMEDIATE` transaction, or serialize all
snapshot writes through a per-event-loop lock. Cover two concurrent `snapshot_now()` calls
and assert a single row is written.

## Verification

Reviewed `git diff HEAD` and every untracked file. `git diff --check HEAD` reported no
whitespace errors. The backend suite passes: `cd backend && uv run pytest -q` — **185
passed**.
