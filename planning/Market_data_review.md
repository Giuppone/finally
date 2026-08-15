# Market Data Backend Review

*Reviewed 2026-08-15. Scope: `backend/app/market/` (cache, models, source, symbols, deps, seeds,
simulator, massive, service, routes, `__init__`) against `planning/MARKET_DATA_DESIGN.md`,
`planning/MARKET_INTERFACE.md`, `planning/MARKET_SIMULATOR.md`, `planning/MASSIVE_API.md`, and
`planning/PLAN.md` §§3, 6, 8, 12, 13. This is a deeper, narrower follow-up to
`planning/Back_end_review.md`, which already covers the trade/portfolio/reset layer — that
document's two open findings (reset not serialized with `trade_lock`; concurrent snapshot writers
can duplicate P&L rows) are not re-litigated here except where they touch market data directly.*

**Test suite**: `cd backend && uv run pytest -q` → **185 passed**, 0 failures.
Market-data-scoped files alone (`test_simulator.py`, `test_massive.py`, `test_service.py`,
`test_cache.py`, `test_symbols.py`, `test_sources.py`, `test_factory.py`) → **114 passed**, 0
failures, 0 warnings under `-W error`.

## Summary

The market-data package is the strongest part of this codebase. It does not just implement
PLAN.md §6 — it correctly implements a **more sophisticated three-mode design**
(`SIMULATED`/`ANCHORED`/`LIVE`) documented in `MARKET_DATA_DESIGN.md`, which exists because live
probing showed PLAN.md §5's literal "key set → real data" binary is unachievable against this
project's Basic-tier Massive key (both snapshot endpoints 403). Every file matches its design-doc
counterpart closely enough that diffing them line-by-line finds only deliberate, well-reasoned
improvements — never a silent deviation. Test coverage is unusually thorough: it exceeds every
item in the design doc's own §14 test list, including the harder-to-motivate ones (positive-
definiteness under a bad `SECTOR_RHO` edit, sliding-window vs. token-bucket rate limiting,
weekend/holiday session-roll edge cases).

One genuine, previously-unreported bug survived this depth of review (P2 below): `add_ticker()`
silently perturbs every *other* already-tracked ticker's simulated price path, which quietly
breaks the `SIM_SEED` bit-reproducibility guarantee (design doc D13) the moment a ticker is added
mid-run — exactly the flow (LLM auto-add, watchlist-add) that E2E tests are supposed to be able to
replay deterministically.

## P1 — Findings

None found in the market-data package itself. (The one P1 in `Back_end_review.md` — reset not
serialized with `trade_lock` — does call `service.sync_tracked()` per `routes.py:137-138`, but the
bug is in the missing lock around the *reset* operation, not in `sync_tracked` or anything reviewed
here; see that document.)

## P2 — Findings

### 1. `add_ticker()` silently advances every other tracked ticker's simulated path, breaking `SIM_SEED` reproducibility

`MarketDataService.add_ticker` (`backend/app/market/service.py:140-146`) does one "immediate
poll" after priming, so a newly added ticker returns already priced:

```python
with contextlib.suppress(Exception):
    for tick in await self._source.poll([ticker]):
        if tick.ticker == ticker:
            quote = self._cache.apply(tick)
```

Under `SimulatedSource` (`backend/app/market/simulator.py:187-190`), `poll()` is implemented as:

```python
async def poll(self, tickers: list[str]) -> list[Tick]:
    now = time.time()
    wanted = set(tickers)
    return [Tick(t, p, now) for t, p in self._engine.step().items() if t in wanted]
```

`GBMEngine.step()` (`simulator.py:96-117`) has no concept of "requested tickers" — it advances
**every ticker currently registered in the engine** by one tick (consuming one `rng.gauss()` draw
per ticker, plus a jump draw), and `poll()` merely *filters the returned list* down to `wanted`.
The other tickers' new prices are written into `self._engine._price` but never emitted as a `Tick`,
so they are never applied to the cache and never appear in history — until the *next* regular poll,
at which point that ticker's reported price silently reflects two compounded steps of variance
instead of one.

**Verified reproduction:**

```python
service = MarketDataService(SimulatedSource(GBMEngine(seed=42), poll_interval=1000),
                            StaticAnchorProvider(), PriceCache(), Mode.SIMULATED)
await service.start({"MU"})
before = engine.price("MU")          # 877.57
await service.add_ticker("AMD")      # unrelated ticker
after = engine.price("MU")           # 877.5401806686485 — MU moved with no MU tick reported
```

**Consequences:**
- **Breaks D13's determinism guarantee under a realistic flow.** `MARKET_SIMULATOR.md` §7 and
  `test_factory.py::test_sim_seed_makes_a_run_reproducible` both promise/verify that a seeded run
  is bit-reproducible — but only by driving `poll()` directly on a fixed ticker set. The moment a
  ticker is added mid-run (which is exactly what PLAN.md §9's LLM auto-add path does on every
  trade of an unwatched symbol, and what a manual watchlist-add does), every other tracked
  ticker's RNG draw sequence is perturbed by an extra, wall-clock-unscheduled step. Two identical
  `SIM_SEED` runs that add tickers at slightly different real-time offsets (a near-certainty, since
  the background poll loop and `add_ticker`'s manual poll are not synchronized) will diverge from
  that point on — undermining the exact guarantee `SIM_SEED` exists to provide for `LLM_MOCK=true`
  E2E runs (PLAN.md §12, design doc D13).
- **Slight, silent volatility inflation.** Whenever tickers are added over the life of a session,
  already-tracked tickers accumulate extra unscheduled steps not accounted for in the `dt`
  calibration (`MARKET_SIMULATOR.md` §1), which assumes one `step()` per `poll_interval` of
  elapsed wall-clock time. In practice this is small (one extra step per add, against ~2
  steps/second of legitimate ticks) and very unlikely to be visually detectable, but it is a
  real deviation from the calibration's assumption.
- Not currently caught by any test: `grep -rn "add_ticker" tests/` shows every `add_ticker` test
  in `test_service.py` uses a single tracked ticker at a time, so the cross-ticker perturbation
  is invisible to the suite.

**Fix direction:** give `GBMEngine` a `step(tickers: Iterable[str] | None = None)` that only
advances the requested subset (or, more simply, have `SimulatedSource.poll()` step only the
tickers it was asked to report, since in the steady-state loop `tickers` already equals every
registered ticker — the only caller that ever requests a strict subset is `add_ticker`'s
single-ticker immediate poll). Add a regression test: seed two identical services, add a second
ticker to only one of them between two polls of a shared first ticker, assert the first ticker's
path is unaffected by the unrelated add.

## P3 — Notes (not defects)

- **PLAN.md itself was never updated with the three-mode decision.** `MARKET_INTERFACE.md` §1 and
  `MARKET_DATA_DESIGN.md` §1 both explicitly state that PLAN.md §5's binary
  "`MASSIVE_API_KEY` set → real data" is "too coarse" and describe the `SIMULATED`/`ANCHORED`/
  `LIVE` resolution as a decision that closes the gap — but unlike every other resolved question,
  this one was never folded back into PLAN.md §13's decision record. An agent who reads only
  PLAN.md (the document `CLAUDE.md` names as authoritative) would still expect a binary switch and
  would not know `ANCHORED` mode exists, why `MODE` is deliberately not an env var, or that
  `open_price` under Massive means "previous close," not "today's open" as PLAN.md §6 literally
  says. Worth adding a §13 row pointing at `MARKET_DATA_DESIGN.md` §1–§2, the same way other
  cross-cutting decisions are recorded there.
- **`source.py`'s `AnchorProvider.is_known` takes a `session_date` parameter** the design doc's
  `MARKET_INTERFACE.md` §3.1 sketch didn't have (`async def is_known(self, ticker) -> bool`).
  This is a deliberate, documented improvement (`source.py:32-38`, and exercised by
  `test_massive.py::test_cold_validate_then_add_costs_one_grouped_request`) that avoids a
  double-load of the ~12,400-symbol universe on a cold validate-then-add flow. Noted only because
  it's a real interface deviation from the written design, not because it's wrong — it's strictly
  better and every call site was updated consistently (`service.py:116-119`, `routes.py`'s
  watchlist-add via `app/routes.py:66`).
- **`RateLimiter` is a sliding window, not the token bucket `MARKET_DATA_DESIGN.md` §8.1
  sketches.** `massive.py:32-60` explains why in its own docstring: a token bucket starting full
  lets a sixth request through ~12s into the window, which is six requests inside a rolling
  minute against a limit advertised as five — exactly wrong for the startup burst (one entitlement
  probe plus up to seven grouped-daily walkback calls). Verified correct by
  `test_massive.py::test_rate_limiter_never_exceeds_the_rate_in_a_rolling_window`, which is written
  specifically to fail against a token-bucket implementation. A genuine improvement over the
  design doc, not a regression.
- **`sync_tracked` holds its lock across both the removal and addition halves**
  (`service.py:159-165`), which is *tighter* than `MARKET_INTERFACE.md` §6's sketch (which does
  `if added: await self._track(added)` unlocked, then removals — two separate critical sections).
  The current code's own docstring explains why: releasing the lock between the two halves lets
  two concurrent reconciliations each compute their addition against the same emptied set,
  converging on the union of both requests instead of the last one. This is verified by
  `test_service.py::test_concurrent_reconciliations_converge_on_the_last_desired_set`, which is
  written to fail under the design doc's original two-phase-unlocked version. Good catch, already
  fixed — flagged here only so it's not miscounted as a deviation.
- **`HybridLiveSource.poll()` in the open-market branch does not fall back to the simulator for
  individual tickers Massive fails to price that tick.** If `MassiveLiveSource.poll()` returns
  fewer ticks than requested (e.g. one symbol's snapshot carries an `error`, per
  `massive.py:256-259`), those tickers simply get no tick that cycle — not even a simulated one —
  whereas the closed-market branch would have supplied motion for all of them. Effect is a
  transient stale-not-wrong price for that one ticker on that one poll, self-heals next poll;
  low severity given `LIVE` mode is unreachable on this project's key, but worth a one-line comment
  if someone runs this on a Starter+ key later.
- **`GBMEngine.step()` unconditionally advances every registered ticker regardless of what
  `poll()` was asked to report** (see P2 above) is also, independently, a minor efficiency note
  outside the P2 correctness concern: on a watchlist that grows large, every `add_ticker` call
  costs one Cholesky-weighted Gaussian draw per already-tracked ticker for no visible effect.
  Immaterial at the ~30-ticker scale PLAN.md targets.

## What's implemented well

- **Cache invariants are exact and thoroughly tested.** `open_price` never moves on `apply()`
  (`cache.py:54-66`, `test_cache.py:12-20`), `day_change_pct` guards the zero-anchor divide
  (`models.py:49-53`, `test_cache.py:50-54`), `version` is monotonic and gates SSE emission
  correctly (`cache.py:22-25`, `routes.py:72-84`), and `history()`'s subsampling always ends on
  the live point (`cache.py:111-124`, `test_cache.py:57-65`) rather than truncating to the last N
  ticks — which is what makes a 60-point sparkline span the whole ring buffer instead of the last
  30 seconds.
- **The volatility budget is correct and covered from multiple angles.** `diffusion_sigma`
  (`simulator.py:29-36`) subtracts jump variance from target variance rather than the archived
  design's additive bug (16× too much vol); `test_volatility_budget_is_exact`,
  `test_jump_variance_matches_the_documented_value`, and
  `test_realised_volatility_lands_near_target` (which actually simulates 400k ticks and measures
  realized σ from log-returns) all independently verify the arithmetic in `MARKET_SIMULATOR.md`
  §5's derivation.
- **Correlation structure is measured against the real matrix, not asserted.**
  `test_correlation_recovers_the_sector_block` and `test_correlation_recovers_a_near_independent_pair`
  simulate 20,000 ticks with jumps switched off and check the *realized* correlation lands within
  0.05 of the target — this is testing the Cholesky factorization end-to-end, not just that the
  lookup table returns the right number.
- **The Massive integration correctly encodes every hard-won fact from `MASSIVE_API.md`**: the
  grouped-daily walkback starts at yesterday, not today (`massive.py:191`, matching the
  documented reasoning about Basic's EOD-only data and Starter+'s in-progress bar); the
  `get_previous_close_agg` runtime-list-vs-annotated-object caveat is handled and commented
  (`massive.py:211-213`); a bad-ticker-inside-the-results-array is checked via `snap.error` before
  reading `snap.session` (`massive.py:256-259`); and `list_universal_snapshots` is called with an
  explicit `limit=250` to avoid the documented default-of-10 silent truncation (`massive.py:243`,
  verified by `test_live_source_requests_the_full_chunk_limit`).
- **The entitlement probe and full mode-detection factory never raise**, verified by
  `test_factory.py`'s parametrized sweep over 403/401/inconclusive-exception/malformed-env-var
  cases — including a case where the `massive` package itself fails to import
  (`test_a_gateway_that_cannot_be_constructed_degrades`), which is a failure mode the design doc
  discusses but doesn't explicitly list a test for.
- **Session rollover handles the cases that are easy to get subtly wrong**: it rolls at 09:30 ET
  (not midnight), it does not invent Saturday/Sunday session labels that would burn a walkback
  request three times over a weekend (`service.py:30-38`, `current_session_date`), and it detects
  a holiday roll (anchor unchanged) and skips the rebase rather than faking an overnight gap on a
  day the market never opened (`service.py:210-219`,
  `test_a_holiday_roll_does_not_fake_an_overnight_gap`) — a case not called out explicitly in
  `MARKET_DATA_DESIGN.md` §9.3 at all, so this is implementation-level thinking beyond the spec.
- **Both sources are proven to satisfy one interface, not just declared to.**
  `test_sources.py::test_source_conformance` is parametrized directly over `SimulatedSource` and a
  stub-backed `MassiveLiveSource`, asserting identical behavioral contracts (positive/finite
  prices, empty-request → empty-result, idempotent `release()`) — this is the test that makes "the
  simulator and the API look identical downstream" a verified fact per PLAN.md §12's requirement
  that "both implementations conform to the abstract interface."
- **The SSE wire contract matches `MARKET_DATA_DESIGN.md` §11.1 exactly**: `retry: 1000` first,
  `hello` then `prices` frames sharing one envelope shape, emission gated strictly on
  `cache.version` change with a 15s ping keepalive otherwise (`routes.py:54-85`), verified by
  `test_stream_opens_with_retry_then_hello`, `test_stream_emits_one_frame_per_cache_change`, and
  `test_stream_is_silent_while_the_cache_is_unchanged`.
- **Ticker normalization is applied consistently at every boundary** — REST (`routes.py`'s
  watchlist and history endpoints), the market package's own history routes
  (`market/routes.py:100-103,124-126`) — with `COLLATE NOCASE` as a schema-level backstop
  (`schema.sql:6`), matching design doc D8 and closing PLAN.md-review finding B1.

## Not directly reviewed (out of scope for this pass)

- Trade execution, reset, and snapshot logic in `portfolio.py`/`routes.py` — covered in
  `planning/Back_end_review.md`, including the two still-open concurrency findings there.
- Chat/LLM integration, frontend, Docker — not yet built (see `Back_end_review.md`'s
  "Not yet implemented" section, still accurate as of this pass).
