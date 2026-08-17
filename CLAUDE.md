# FinAlly Project - the Finance Ally

All project documentation is in the `planning` directory.

The key document is PLAN.md included in full below. Section 13 is a decision record resolving open questions from doc reviews — those decisions are already reflected in sections 1–12.

## Current state (verified 2026-08-16)

**All twelve sections of PLAN.md are implemented.** The app builds, runs in Docker and serves a working trading terminal at `http://localhost:8000`.

- **Backend** (§6–§9): database + schema, the market-data package (three modes — see PLAN.md §13 item 9), SSE streaming, price cache with ring buffer, trading/portfolio/watchlist endpoints, portfolio-session save/load (`GET`/`POST /api/session`), the analytics package (`app/analytics/` — risk decomposition and rebalance suggestions, PORTFOLIO_ANALYTICS.md), 30s P&L snapshots, and the LLM chat package (`app/chat/`). `cd backend && uv run pytest -q` → **314 passed**.
- **Frontend** (§10): `frontend/` is a Next.js 16 + React 19 + Tailwind static export using **Recharts** as the single charting dependency — the open question in PLAN.md §13 "Still open" is now decided, because Recharts covers the line chart, the sparklines *and* the treemap, while Lightweight Charts has no treemap.
- **Packaging** (§11, §12): multi-stage `Dockerfile`, `docker-compose.yml`, start/stop scripts for macOS and Windows, and a Playwright E2E suite in `test/` (**37 specs, all passing**).

Design notes from the deleted first pass are in `planning/archive/`; consult them only when required. The current design docs are `MARKET_DATA_DESIGN.md`, `MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md` and `MASSIVE_API.md`. Code reviews live in `Back_end_review.md` and `Market_data_review.md` — the three findings open at the time of those reviews (reset not serialised, duplicate snapshot rows, `add_ticker` perturbing other GBM paths) were **fixed on 2026-08-15**, each with a regression test.

## Environment notes

**Node and npm are not installed on this machine; Docker is.** Build and test the frontend in a container:

```bash
docker run --rm -v "$PWD/frontend":/app -w /app node:20 npm ci
docker run --rm -v "$PWD/frontend":/app -w /app node:20 npm run build
```

The project `.env` holds a **Basic-tier** `MASSIVE_API_KEY`, so a local run resolves to `ANCHORED` — real previous closes with simulated intraday motion — not `LIVE`. That is expected, and the header badge says so.

## Running it

```bash
# Whole app in Docker (frontend + backend + SQLite volume)
docker build -t finally . && docker run -d --name finally -p 8000:8000 --env-file .env -v finally-data:/app/db finally
./scripts/start_mac.sh --open                # or .\scripts\start_windows.ps1 -Open

# Backend only — also serves frontend/out if it has been built
cd backend
uv run uvicorn app.main:app --port 8000      # needs env vars; see PLAN.md §5
uv run pytest -q                             # 314 tests
uv run market_data_demo.py                   # live terminal dashboard, simulator
uv run market_data_demo.py --live            # same, against the real Massive key

# E2E against the real container, LLM_MOCK=true — 37 specs (close other containers first;
# a second app instance competing for CPU roughly doubles the wall time)
docker compose -f test/docker-compose.test.yml up --build --exit-code-from playwright
```

The Playwright container shares the app's network namespace and drives it on `127.0.0.1`. That is
load-bearing: current Chromium force-upgrades `http://` to `https://` for any non-loopback host, so
the older `http://app:8000` base URL fails **every** spec with `ERR_SSL_PROTOCOL_ERROR`. Launch flags
do not fix it. Consequence: `context.setOffline()` cannot cut the SSE stream over loopback, so
`sse-resilience.spec.ts` intercepts the stream route instead.

## Portfolio harness (planning/REBALANCE_TEST_HARNESS.md)

Put the account into a known state, or snapshot and restore one. All four wrap
`backend/scripts/portfolio_tool.py` (stdlib only) and go through the public API, so the trade lock,
validation and snapshots apply exactly as they do for a human clicking Buy.

```powershell
# PowerShell (this machine) — .sh twins exist for Git Bash, macOS and CI
.\scripts\equal_weight_portfolio.ps1 --yes           # equal DOLLAR weight = unequal RISK weight
.\scripts\start_random_portfolio.ps1 --yes --seed 7  # reproducible, deliberately lopsided
.\scripts\save_session.ps1 --name lopsided           # -> sessions\lopsided.json
.\scripts\load_session.ps1 --name lopsided --yes     # exact restore, avg_cost and all
```

Flags are the Python tool's, not PowerShell's — `--yes`, not `-Yes`. The wrappers deliberately have no
`param()` block and do not set `$ErrorActionPreference = "Stop"`; both would mangle passthrough. Any
shell can also skip the wrappers: `uv run python backend/scripts/portfolio_tool.py equal --yes`.

These scripts do **not** start the app - they seed a portfolio into a running one, so bring the
container up first (`.\scripts\start_windows.ps1`). The seeders and `load` reset/replace the portfolio,
so they prompt unless `--yes`; `--dry-run` writes nothing at all. Add `--base http://localhost:PORT`
when the app is not on 8000. Everything the tool prints is ASCII — a
Windows cp1252 console cannot encode `≈` and crashed mid-run before that was fixed.

The backend reads `os.environ` only and never parses `.env` (§5), so load the file into the environment before launching or it starts in `SIMULATED`. It also **fails fast** at startup without `OPENROUTER_API_KEY` unless `LLM_MOCK=true`.

@planning/PLAN.md