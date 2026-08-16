# FinAlly Project - the Finance Ally

All project documentation is in the `planning` directory.

The key document is PLAN.md included in full below. Section 13 is a decision record resolving open questions from doc reviews — those decisions are already reflected in sections 1–12.

## Current state (verified 2026-08-16)

**All twelve sections of PLAN.md are implemented.** The app builds, runs in Docker and serves a working trading terminal at `http://localhost:8000`.

- **Backend** (§6–§9): database + schema, the market-data package (three modes — see PLAN.md §13 item 9), SSE streaming, price cache with ring buffer, trading/portfolio/watchlist endpoints, 30s P&L snapshots, and the LLM chat package (`app/chat/`). `cd backend && uv run pytest -q` → **224 passed**.
- **Frontend** (§10): `frontend/` is a Next.js 16 + React 19 + Tailwind static export using **Recharts** as the single charting dependency — the open question in PLAN.md §13 "Still open" is now decided, because Recharts covers the line chart, the sparklines *and* the treemap, while Lightweight Charts has no treemap.
- **Packaging** (§11, §12): multi-stage `Dockerfile`, `docker-compose.yml`, start/stop scripts for macOS and Windows, and a Playwright E2E suite in `test/`.

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
uv run pytest -q                             # 224 tests
uv run market_data_demo.py                   # live terminal dashboard, simulator
uv run market_data_demo.py --live            # same, against the real Massive key

# E2E against the real container, LLM_MOCK=true
docker compose -f test/docker-compose.test.yml up --build --exit-code-from playwright
```

The backend reads `os.environ` only and never parses `.env` (§5), so load the file into the environment before launching or it starts in `SIMULATED`. It also **fails fast** at startup without `OPENROUTER_API_KEY` unless `LLM_MOCK=true`.

@planning/PLAN.md