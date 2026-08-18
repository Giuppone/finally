# Review: working-tree changes since `HEAD`

## Findings

### P1 — `load_list` resets the account before validating every requested ticker

[`cmd_build`](../backend/scripts/portfolio_tool.py#L617) calls
`/api/portfolio/reset` before it adds the list's unwatched tickers at
[lines 625–632](../backend/scripts/portfolio_tool.py#L625). A typo or symbol the market-data
provider cannot anchor therefore fails only after the reset, leaving the user with an empty
portfolio. This conflicts with the command's documented promise that a failed build will not
leave a partially constructed portfolio. Preflight every ticker and price before reset, or save
and restore the prior session if preparation fails. Add a regression test containing one valid
and one invalid ticker.

### P1 — Broker conversion can silently import an incomplete account

[`parse_broker`](../backend/scripts/portfolio_tool.py#L398) scans the whole export with
`finditer`. If a row stops matching the strict five-line expression—for example after a broker
layout change—`finditer` skips it and resumes at the next matching ticker. The converter then
normalizes the remaining rows and reports success; the arithmetic warning only covers rows that
matched but did not reconcile. Require complete consumption of the holdings section, or reject
and report unmatched non-whitespace text, rather than generating a partial allocation.

### P2 — An all-local broker export crashes with an unhandled division by zero

After filtering non-CEDEAR rows, [`cmd_broker`](../backend/scripts/portfolio_tool.py#L555) does
not check that `keep` is non-empty. `kept_total` is then zero and the list comprehension at
[line 562](../backend/scripts/portfolio_tool.py#L562) raises `ZeroDivisionError`, bypassing the
CLI's `ApiError` handling. Fail with a clear message before calculating weights; `--keep-local`
should only be suggested if those tickers can be traded in the target market.

### P2 — Exactly-zero measured correlations are discarded as though absent

[`sector_rho`](../backend/app/market/simulator.py#L166) uses `measured_a or measured_b` to select
a calibrated correlation. A valid `0.0`/`-0.0` is falsy, so the code falls back to the sector or
default value instead. The generated data already contains `("AMD", "LMT"): -0.00` and
`("MSFT", "SNDK"): -0.00`; both are currently treated as the default `0.35`, materially changing
the covariance matrix. Check dictionary membership (or use an explicit `is not None` lookup) and
add a zero-correlation test.

### P2 — A malformed cache timestamp can crash recalibration instead of being refreshed

[`is_fresh`](../backend/scripts/calibrate_market.py#L212) subtracts an aware UTC time from
`datetime.fromisoformat(fetched)` but catches only `ValueError`. A cache entry with an otherwise
valid but timezone-naive timestamp raises `TypeError` and aborts the command. Treat both malformed
and timezone-naive timestamps as stale (or normalize naive timestamps) and cover that cache
input in the existing test suite.

### P2 — A real brokerage account export is present as repository content

[`suggested/sugested.txt`](../suggested/sugested.txt) includes individual positions and ARS market
values from a real account. It is untracked today, but nothing prevents it from being committed or
distributed with the project. Remove/redact it in favor of a synthetic fixture and add a scoped
ignore rule for local broker exports if this data is not intended to be public.

### P3 — The new quick-start path contains a backspace control character

[`CLAUDE.md`](../CLAUDE.md#L67) renders the broker output as `suggested` followed by a backspace
character and `roker.txt`, rather than the intended `suggested\\broker.txt`. Replace the control
character with a literal backslash so the documented path can be copied correctly.

## Verification

- `cd backend && uv run pytest -q` — **373 passed**.
- `cd backend && uv run pytest -q tests/test_portfolio_tool.py tests/test_calibrate.py tests/test_simulator.py` — **95 passed**.
- `git diff --check` — no whitespace errors.
- Directly reproduced the naive-timestamp `TypeError` in `is_fresh`; inspected the generated
  `-0.00` correlation entries and the reset-before-watchlist-add execution order.
