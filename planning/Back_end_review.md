# Backend Review

*Reviewed 2026-08-15. Scope: `backend/` against `planning/PLAN.md` §§3, 6–8, 12. `CLAUDE.md` /
PLAN.md's decision record (§13 item 1) both describe `backend/` as empty and rebuilt from
scratch — that is stale. The market-data + portfolio slice has already been rebuilt and is
substantially more capable than the PLAN.md minimum (LIVE/ANCHORED/SIMULATED modes, jump-
diffusion GBM with sector correlation, a Massive REST client with rate limiting and session
rollover). Chat/LLM (§9) and the frontend are not started — expected at this stage, listed
under "Not yet implemented" below rather than as defects.*

**Test suite**: `cd backend && uv run pytest -q` → **185 passed**, 0 failures, 0 warnings.

## Summary

The implementation is careful, well above the PLAN.md bar, and cites its own reasoning
in-line (docstrings reference a `Review.md` and `MARKET_SIMULATOR.md`/`MARKET_DATA_DESIGN.md`
design doc for nearly every non-obvious decision). Schema, seed data, endpoint shapes, cache
fields, and the tracked-set-is-a-union rule all match PLAN.md exactly. The two concurrency
bugs flagged in a prior working-tree review (`planning/Review-commit.md`) are **still present**
in the current code — that review was against a checkpoint that predates the current
`portfolio.py`/`routes.py`, and neither fix landed since. Everything else found is low-severity
polish or an intentional, reasonable deviation.

## P1 — Findings

### 1. `POST /api/portfolio/reset` is not serialized with trade execution

`post_reset` (`backend/app/routes.py:128-139`) calls `db.reset`, then `db.tracked_tickers`,
then `service.sync_tracked` — none of it under `portfolio.trade_lock()`. `execute_trade`
(`backend/app/portfolio.py:204-212`) *does* take that lock around its own DB write and
`sync_tracked` call, but a lock only one side takes doesn't serialize anything.

**Failure scenario**: a trade has already read its fill price and is inside
`trade_lock()` (`portfolio.py:204`) when a reset request runs concurrently. Two bad
orderings are both reachable:
- Reset's `db.reset()` fires, then the in-flight trade's `_apply` commits on top of the
  just-cleared tables — a position, a trade row and a non-$10,000 cash balance survive in
  what the reset response calls a fresh account.
- The trade commits and calls `sync_tracked` first; reset's `db.tracked_tickers` read (taken
  *after* `db.reset()` already deleted the position) returns only the seed watchlist, so
  `sync_tracked` evicts the just-traded ticker from the cache — it stops pricing even though
  `db.reset()` may have already wiped the position that would have justified evicting it, or
  (in the other interleaving) a position that reset itself just recreated via `force=True`
  seeding never gets tracked.

**Fix**: acquire `portfolio.trade_lock()` for the full reset operation — the DB reset, the
tracked-set read, and `sync_tracked` — same as `execute_trade` does. This was called out in
`planning/Review-commit.md` P1 against an earlier snapshot of this code; it was not carried
forward into the current `routes.py`.

**Test gap**: no test exercises concurrent reset + trade. Add one asserting the final state
is one coherent outcome (either the trade's effects or a clean reset, never a mix) and that
any surviving position is in the tracked set and priced. (`grep` over `tests/` confirms no
`reset`-adjacent concurrency test exists — `test_reset_restores_a_fresh_account` in both
`tests/test_db.py:109` and `tests/test_trading_routes.py:178` are sequential-only.)

## P2 — Findings

### 2. Concurrent snapshot writers can still produce duplicate, unchanged history rows

`_snapshot` (`backend/app/portfolio.py:301-314`) reads `db.last_snapshot`, compares to the
freshly computed `total_value`, and conditionally calls `db.record_snapshot` — as two
separate statements on an autocommit connection (`db.transaction` is not used here), with no
lock around the pair.

`snapshot_now` is called from two independent paths that are **not** mutually exclusive:
`SnapshotTask`'s 30-second `snapshot_loop` (`portfolio.py:317-327`), and the trade-triggered
call inside `execute_trade` (`portfolio.py:212`) — the latter *is* inside `trade_lock()`, but
`trade_lock` guards trade execution, not snapshot writes, and the periodic loop never takes
it. Two `db.run` calls execute on separate `asyncio.to_thread` worker threads and can
genuinely overlap.

**Failure scenario**: the 30s tick and a trade's post-commit snapshot land within the same
window. Both read the same `last_snapshot` row, both compute the same new `total_value`,
both find it "changed" relative to that stale read, and both insert — one avoidable extra
point on the P&L chart. Rare in practice (needs sub-millisecond timing overlap across two
threads) but the de-duplication comment at `portfolio.py:310-311` states this can't happen,
and as written it still can.

**Fix**: wrap the read-compare-insert in one `db.transaction(conn)` (SQLite's
`BEGIN IMMEDIATE` will make the second writer block and then re-read post-commit — the
dedupe check still works, it's just serialized), or take a dedicated snapshot lock analogous
to `trade_lock`. Flagged as P2 not P1 (as `Review-commit.md` also rated it) because the
failure mode is an extra chart point, not a wrong number or lost data — but it is genuinely
still unfixed, not superseded.

**Test gap**: `tests/test_portfolio.py:236-267` covers snapshot skip-when-unpriced and
skip-when-unchanged sequentially; no test drives two concurrent `snapshot_now()` calls to
assert a single row is written.

## P3 — Notes (not defects)

- **SSE cadence is event-driven, not polled.** `_price_events`
  (`backend/app/market/routes.py:65-85`) only emits a `prices` frame when
  `cache.version` changes, falling back to a `: ping` comment every 15s otherwise, rather than
  unconditionally emitting every 500ms as PLAN.md §6 literally reads ("pushes... at a regular
  cadence (~500ms)"). Functionally equivalent when the simulator is the source (it ticks
  every 500ms, so version changes every 500ms), and strictly better under Massive polling
  (no reason to emit an unchanged quote every 500ms when the underlying poll is 15s). Worth
  confirming the frontend's reconnect/staleness logic doesn't assume a literal 500ms cadence
  regardless of `healthy`/`poll_interval_s`, since those are exactly the fields the `hello`
  frame exists to communicate.
- **`POST /api/portfolio/reset` isn't in PLAN.md's §8 endpoint table.** It's a reasonable,
  well-justified addition (`routes.py:130-135` cites the E2E fresh-start scenario and the
  "LLM drains the account" demo-recovery case) but is undocumented in the spec — worth adding
  to PLAN.md §8 now that it exists, so the frontend/E2E agents know it's part of the contract
  (and so the P1 above is visibly "reset must be safe under concurrency," not a surprise).
- **`GET /api/prices/history` (bulk, comma-separated tickers)** exists alongside the spec'd
  per-ticker `GET /api/prices/{ticker}/history` (`market/routes.py:92-112`). Also a reasonable,
  undocumented addition — built for sparkline seeding in one round trip instead of N. Same
  suggestion: fold into PLAN.md §8.
- **Schema is ahead of the currently-built surface.** `chat_messages` exists in
  `schema.sql:53-60` and is already wired into `db.reset` (`db.py:207-208`) and
  `db.stats`'s required-table check (`db.py:225-226`), even though no chat endpoint reads or
  writes it yet. Fine — matches PLAN.md's schema exactly — just noting it's inert until §9
  lands.
- **`init_db` runs at lifespan startup, not lazily on first request** (`main.py:31`,
  `schema/__init__.py:27-37`). PLAN.md §7 says "on startup (or first request)" so this is
  within the spec's stated options, and the in-code rationale (the market service needs the
  watchlist before its own startup) is sound.

## What's implemented well

- **Schema** (`schema/schema.sql`) matches PLAN.md §7 field-for-field, plus sensible indexes
  on every `(user_id, timestamp)` pair and `COLLATE NOCASE` on ticker columns as a backstop
  to `normalize_ticker` (defense in depth against the classic `aapl`/`AAPL` duplicate-row
  bug).
- **Seed data** (`market/seeds.py:8-15`) is the corrected ten-ticker set from PLAN.md §7 —
  `INTC` and `MU`, not the `INTEL`/`Micron` typos in the repo's `bck.txt` scratch notes.
- **Tracked-set union rule** (§6, §13 item 4) is implemented correctly and consistently:
  `db.tracked_tickers` unions watchlist and `quantity > 1e-9` positions
  (`db.py:90-104`), and every mutation path (`add_to_watchlist`, `remove_from_watchlist`,
  `execute_trade`, `reset`) recomputes and calls `service.sync_tracked` afterward.
- **Cache entry fields** (`market/models.py:21-53`) — `price`, `prev_price`, `open_price`,
  `ts`, bounded `history` deque — match §6 exactly, including the derived `direction` and
  `day_change_pct` computed off `open_price` rather than `prev_price`, per §13 item 3.
- **Trade execution** (`portfolio.py:173-276`) does read-validate-write in one
  `BEGIN IMMEDIATE` transaction, handles the dust-quantity phantom-row case on full sells,
  and auto-adds an unwatched ticker to the watchlist on buy (§9's rule, generalized correctly
  to all trade entry points, not just the not-yet-built LLM path).
- **Concurrent-trade correctness is otherwise solid**: `tests/test_portfolio.py:172-199`
  exercises concurrent buys that would overdraw cash and concurrent sells that would oversell,
  and both are correctly rejected via `trade_lock` + the transactional `_apply`. The gap is
  specifically reset-vs-trade and snapshot-vs-snapshot, not trade-vs-trade.
- **Market data package is materially beyond the PLAN.md minimum**: correlated jump-diffusion
  GBM with a Cholesky-factored sector correlation matrix (`market/simulator.py`), a Massive
  REST client with a correct sliding-window rate limiter (not a token bucket — the module
  docstring at `massive.py:32-43` explains why that distinction matters against a 5 req/min
  ceiling), automatic entitlement detection that degrades gracefully (`SIMULATED` /
  `ANCHORED` / `LIVE`), and session-rollover handling for the ET trading day. All backed by
  focused unit tests (`test_simulator.py`, `test_massive.py`, `test_service.py`).
- **Single-worker invariant is respected**: the cache is mutated only from the one event-loop
  thread, `db.py` explicitly offloads every SQLite call via `asyncio.to_thread` with its own
  connection (avoiding the blocking-call-stalls-every-SSE-client trap called out in its own
  docstring), and nothing in the reviewed code assumes multiple processes or workers.
- **185/185 tests pass**, with real coverage of the trickier spec requirements: GBM volatility
  budget and positive-definiteness of the correlation matrix (`test_simulator.py`), Massive
  response parsing including the nanosecond-timestamp and nonstandard-error-shape edge cases
  (`test_massive.py`), and trade-edge-case rejection (insufficient cash, overselling,
  fractional dust) in `test_portfolio.py`.

## Not yet implemented (expected at this stage, not defects)

- Chat / LLM integration (§9): no `POST /api/chat`, no `GET /api/chat/history`, no LiteLLM/
  OpenRouter wiring, no `OPENROUTER_API_KEY` fail-fast check, no `LLM_MOCK` handling. The
  `chat_messages` table exists in schema but nothing reads or writes it yet.
- Frontend (§10) and its static-file mount in `main.py` — the file even has a comment
  (`main.py:65-67`) describing where the `StaticFiles(html=True)` mount will go once it
  exists.
- Docker/deploy artifacts (§11): no `Dockerfile`, no `scripts/start_mac.sh` etc. reviewed as
  part of this pass (out of scope — this review is backend code only).
- E2E test infrastructure (§12's `test/` + `docker-compose.test.yml`) — not present; only
  backend unit tests were in scope here.
