# Working-tree review (`HEAD` plus untracked files)

## Findings

### P1 - Concurrent `sync_tracked()` calls can leave the service tracking neither caller's intended set

[`MARKET_DATA_DESIGN.md`](MARKET_DATA_DESIGN.md#L1258) takes `self._lock` only while removing symbols, then releases it before calling `_track(added)`. `_track()` itself performs an awaited anchor lookup and mutates `_tracked` without the lock ([line 1271](MARKET_DATA_DESIGN.md#L1271)). Two concurrent reconciliations for `{A}` and `{B}` can therefore both compute their additions from the same old set and then each add its symbol; the final set becomes `{A, B}`, even though the later reconciliation intended `{B}`. An `add_ticker()` racing a reconciliation has the same duplicate-prime / stale-membership problem.

Make reconciliation atomic with respect to the complete desired set. For example, serialize the full diff-and-add operation under one service lock (with a separate in-flight task or a carefully designed two-phase commit if network waits must occur outside the lock), and have `_track()` require that lock.

### P1 - Calendar-day session rollover reanchors and consumes Massive requests during weekends and holidays

[`current_session_date()`](MARKET_DATA_DESIGN.md#L1136) deliberately returns Saturday and Sunday labels, and `_maybe_roll_session()` refreshes anchors whenever that label changes ([line 1307](MARKET_DATA_DESIGN.md#L1307)). The service continues polling the simulator on those days, so an ANCHORED instance will reset its daily change and perform a grouped-daily walkback on Saturday and again on Sunday. On Monday before 09:30 ET it labels the session Sunday and repeats the work, despite the last completed market session still being Friday.

This produces fictitious weekend "sessions", breaks a continuous simulated path by rebasing it to Friday's close multiple times, and spends Basic-tier requests outside a trading session. Derive the session identifier from a trading calendar (or retain the prior trading session until the next valid market open) and only run `refresh()`/reanchor when a new trading session begins.

### P1 - The proposed rate limiter does not enforce the documented 5-requests-per-minute limit

[`RateLimiter`](MARKET_DATA_DESIGN.md#L786) starts with five tokens and refills continuously, so it permits five requests immediately and a sixth about 12 seconds later. That is six requests in a rolling minute. The documented startup path can also make one entitlement probe plus up to seven grouped-daily walkback requests ([line 982](MARKET_DATA_DESIGN.md#L982)), while the key is limited to five per minute.

On a provider that enforces the advertised rolling window this can immediately produce 429s, undermining the design's startup and rollover guarantees. Use a sliding-window limiter (record the last five request times), or conservatively space calls at least 12 seconds apart; explicitly account for the probe and failed walkback attempts in the startup budget.

### P2 - The factory's "never raises" contract is broken by an invalid `SIM_SEED`

[`build_market_service()`](MARKET_DATA_DESIGN.md#L1460) calls `int(sim_seed)` without validation. A malformed deployment value such as `SIM_SEED=abc` raises `ValueError` before either simulated fallback can be built, despite the surrounding documentation promising market-data startup never fails.

Validate the value and log a warning while treating an invalid seed as unset (or fail configuration explicitly and adjust the stated contract). Add a test for the malformed value.

### P2 - The shown source-conformance test cannot run with its shown stub gateway

The `MassiveLiveSource` branch in [`test_source_conformance`](MARKET_DATA_DESIGN.md#L1943) calls `MassiveLiveSource.poll()`, which invokes `gateway.call_list()` ([line 1007](MARKET_DATA_DESIGN.md#L1007)). The supplied `StubGateway` defines only `call()` ([line 1929](MARKET_DATA_DESIGN.md#L1929)). Consequently this test fails with `AttributeError` before it exercises conformance.

Add a `call_list()` implementation to the stub (and return snapshot data through it), or give the live-source test a compatible dedicated fake.

## Verification

Reviewed `git diff HEAD` and untracked files. The sole implementation change is the new `planning/MARKET_DATA_DESIGN.md`; no executable code or test suite was added to run.
