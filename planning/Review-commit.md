# Review: working-tree changes since `HEAD`

## Findings

### P2 — Invalid `--duration` values can silently stop or run the demo forever

[`parse_args`](../backend/market_data_demo.py#L324) accepts every float for
`--duration`, while [`run`](../backend/market_data_demo.py#L281) treats every
truthy value as a bounded duration. Therefore, `--duration -1` exits successfully
immediately, while `--duration nan` and `--duration inf` never satisfy the exit
condition and run until interrupted. This conflicts with the documented contract that
only `0` runs indefinitely and makes simple CLI typos misleading.

Use an argparse type that accepts only finite values greater than or equal to zero.

### P2 — The dashboard's error collector cannot receive market-data errors

[`run`](../backend/market_data_demo.py#L257) sets the root logger level to `CRITICAL`
and then adds `ErrorCollector` to the `app` logger at
[line 258](../backend/market_data_demo.py#L258). `app` has no explicit level, so its
children—including `app.market.service`—inherit `CRITICAL`. Their warning/error records
are rejected before handlers are invoked; the dashboard can consequently show
`DEGRADED` without displaying the exception that caused it.

Set `app` to `WARNING` (and disable propagation if output must remain quiet), or attach
the collector to a logger whose effective level admits those records.

## Verification

Reviewed `git diff HEAD` and the untracked `backend/market_data_demo.py`.
`git diff --check HEAD` reported no whitespace errors. The backend suite passes:
`uv run --directory backend pytest -q` — **191 passed**. Manual CLI verification
confirmed that `--duration -1` exits successfully at `0.0s`; `--interval 0` is
correctly rejected.
