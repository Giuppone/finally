# Review: working-tree changes since `HEAD`

## Findings

### P1 — Exclude raw brokerage exports from the Docker build context

[`example/compras-ventas-fechas.txt`](../example/compras-ventas-fechas.txt) is correctly
ignored by [`.gitignore`](../.gitignore), but `example/` is not excluded by
[`.dockerignore`](../.dockerignore). Every `docker build .` therefore sends this dated,
account-specific trade export (as well as the other files in that directory) to the Docker
daemon/build service. Add `example/` to `.dockerignore`, keeping only the reviewed generated
`backend/calibration/ledger.json` in the image.

### P1 — Do not crash when every ledger row is a currency conversion

[`backend/app/history/reconstruct.py`](../backend/app/history/reconstruct.py#L248) derives the
walk boundaries from `effective` after removing recognized conversion rows. A valid ledger that
contains only an AL30 USD/ARS conversion and an opening US holding leaves `effective` empty, so
`min()`/`max()` raise `ValueError` and both history endpoints return 500. Fall back to the
ledger/snapshot dates (or return an unavailable reconstruction) when no non-conversion rows
remain, and cover this document shape with a test.

### P1 — Prevent stale history fetches from replacing the selected chart

[`frontend/app/page.tsx`](../frontend/app/page.tsx#L89) writes each completed `loadDaily` and
`loadCurve` request directly to the shared `daily`/`curve` state. There is no cancellation or
request-key check. If a user selects A then B before A returns, A can overwrite B's chart while
the header still says B; quick range changes can similarly show MAX data under a 1M label. Guard
the state update with the active ticker/range (or abort superseded requests).

### P2 — Malformed ledger data contradicts the documented graceful-degradation contract

[`backend/app/history/routes.py`](../backend/app/history/routes.py#L98) lets JSON and
`LedgerError` exceptions from `_compute()` propagate. This conflicts with the route's own claim
that a malformed ledger should degrade the history panel rather than fail it; a malformed
committed `ledger.json` makes `/api/history/portfolio` return 500. Catch parse/version errors,
cache an unavailable reconstruction with a warning, and make `/session` return its documented
`no_ledger` response. The current test explicitly expects the exception, so it preserves the
failure instead of checking the advertised behavior.

## Verification

- Inspected `git diff HEAD`, all untracked files, and `git diff --check HEAD` (no whitespace
  errors).
- Ran `backend/.venv/Scripts/python.exe -m pytest -q`: **431 passed**.
- Ran `backend/scripts/portfolio_tool.py ledger --dry-run --json`: the generated ledger
  reconciles all 26 priced tickers and produces a 151-point curve.
- Frontend production build could not run because Node/npm is unavailable in this environment.
