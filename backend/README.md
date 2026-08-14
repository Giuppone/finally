# FinAlly backend

FastAPI application. See `planning/PLAN.md` for the product spec,
`planning/MARKET_DATA_DESIGN.md` for the market-data package, and `planning/Review.md`
for the findings the trading layer implements.

## Development

```bash
cd backend
uv sync
uv run pytest                       # unit tests, no network
uv run uvicorn app.main:app --port 8000 --reload
```

Exactly **one** uvicorn worker. The price cache and the market-data task live in process
memory (PLAN.md §3), so a second worker would get its own cache plus a second writer
racing on the same SQLite file.

## Layout

```
app/
├── main.py         FastAPI app, lifespan wiring, /api/health
├── clock.py        ISO-8601 UTC helpers (the non-market timestamp convention)
├── db.py           SQLite access — one connection per op, all off the event loop
├── schema/         schema.sql + seed data (NOT backend/db/, which the volume shadows)
├── portfolio.py    valuation, trade execution, P&L snapshots
├── routes.py       /api/watchlist/*, /api/portfolio/*
└── market/         market data — the only import surface is `app.market`
```

## Configuration

Read from `os.environ` only; the backend never parses a `.env` file itself (PLAN.md §5).

| Variable | Default | Effect |
|---|---|---|
| `MASSIVE_API_KEY` | *(unset)* | Unset → `SIMULATED`. Set → a startup probe picks `ANCHORED` or `LIVE`. Never fails startup. |
| `FINALLY_DB_PATH` | `<repo>/db/finally.db` | SQLite file. **The container must set `/app/db/finally.db`** — that is where the volume mounts. |
| `SIM_SEED` | *(unset)* | Seeds the simulator RNG for reproducible E2E runs. |
| `MARKET_POLL_INTERVAL_S` | `15` | `LIVE` poll cadence. Ignored in the simulated modes. |
| `MARKET_CLOSED_FALLBACK` | `true` | `LIVE` only: keep the tape moving outside market hours. |

## Modes

The market-data mode is **detected, never configured** — an env var would only let a
deployment lie about what its key can do.

| Mode | Trigger | Price motion | Session anchor |
|---|---|---|---|
| `SIMULATED` | No key | GBM engine | Static seed table |
| `ANCHORED` | Key present, snapshots 403 (**this repo's Basic key**) | GBM engine | Real previous close from Massive |
| `LIVE` | Key present, snapshots entitled (Starter+) | Massive polling | Real previous close |

`GET /api/health` reports the resolved mode, and so does the SSE `hello` frame.
