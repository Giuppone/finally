# FinAlly — AI Trading Workstation

A visually stunning AI-powered trading workstation that streams live market data, simulates portfolio trading, and integrates an LLM chat assistant that can analyze positions and execute trades via natural language.

Built entirely by coding agents as a capstone project for an agentic AI coding course.

## Features

- **Live price streaming** via SSE with green/red flash animations
- **Simulated portfolio** — $10k virtual cash, market orders, instant fills
- **Portfolio visualizations** — heatmap (treemap), P&L chart, positions table
- **AI chat assistant** — analyzes holdings, suggests and auto-executes trades
- **Watchlist management** — track tickers manually or via AI
- **Dark terminal aesthetic** — Bloomberg-inspired, data-dense layout

## Architecture

Single Docker container serving everything on port 8000:

- **Frontend**: Next.js 16 (static export) with TypeScript, Tailwind CSS and Recharts
- **Backend**: FastAPI (Python/uv) with SSE streaming
- **Database**: SQLite, initialized on startup, persisted in a named volume
- **AI**: LiteLLM → OpenRouter (Cerebras inference) with structured outputs
- **Market data**: Built-in GBM simulator (default) or Massive API (optional)

Run it with **exactly one uvicorn worker**. The price cache and the market-data task live in
process memory, so a second worker would hold its own independent cache and race the first
on SQLite writes.

## Quick Start

```bash
cp .env.example .env
# Add your OPENROUTER_API_KEY to .env (or set LLM_MOCK=true to run without one)
```

Then either use the start scripts:

```bash
./scripts/start_mac.sh --open      # macOS / Linux
.\scripts\start_windows.ps1 -Open  # Windows
```

…or drive Docker yourself:

```bash
docker build -t finally .
docker run -d --name finally -p 8000:8000 --env-file .env -v finally-data:/app/db finally

# or: docker compose up --build
```

Open <http://localhost:8000>.

Stop with `./scripts/stop_mac.sh` (or `.\scripts\stop_windows.ps1`). Neither script removes
the data volume — the portfolio, trade history and conversation survive a restart. To wipe
them: `docker volume rm finally-data`.

## Market data modes

`MASSIVE_API_KEY` is **not** a binary switch. On startup the backend probes what the key is
actually entitled to and resolves to one of three modes, degrading rather than failing:

| Mode | When | Session anchor | Intraday motion |
|---|---|---|---|
| `LIVE` | Key entitled to snapshots (Starter+) | Real daily open | Real, polled ~15s |
| `ANCHORED` | Key is aggregates-only (Basic) | **Real previous close** | Simulated (GBM) |
| `SIMULATED` | No key, unusable key, or SDK missing | Seed price | Simulated (GBM) |

The header badge always names the resolved mode. It says `ANCHORED`, not `LIVE`, when the
prices are real closes with simulated movement on top — claiming otherwise would misrepresent
the data you are trading against. There is deliberately no env var to force a mode: a
hand-set mode could select `LIVE` on a key that cannot serve it, which is the exact failure
the probe exists to catch.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes¹ | OpenRouter API key for AI chat |
| `MASSIVE_API_KEY` | No | Massive (Polygon.io) key; omit for the simulator |
| `LLM_MOCK` | No | `true` for deterministic mock LLM replies (testing, CI) |
| `SIM_SEED` | No | Seed the simulator's RNG for a reproducible demo |
| `MARKET_POLL_INTERVAL_S` | No | Seconds between Massive polls in `LIVE` mode (default 15) |
| `MARKET_CLOSED_FALLBACK` | No | Simulate motion when the market is closed (default `true`) |
| `FINALLY_DB_PATH` | No | SQLite path; the image sets `/app/db/finally.db` |
| `FINALLY_STATIC_DIR` | No | Built frontend; the image sets `/app/static` |

¹ Required unless `LLM_MOCK=true`. If it is missing and mock mode is off, the backend
**fails fast at startup** with an error naming the variable — chat is a core feature, and a
silently half-working app is worse than a clear message.

The backend reads `os.environ` only; it never parses `.env` itself. Delivery is via
`--env-file .env` (or compose `env_file:`).

## Development

```bash
# Backend tests
cd backend && uv run pytest -q

# Backend alone (serves frontend/out too, if you have built it)
cd backend && uv run uvicorn app.main:app --port 8000

# Live market-data dashboard in the terminal
cd backend && uv run market_data_demo.py

# Frontend
cd frontend && npm install && npm run build   # static export -> frontend/out
cd frontend && npm run dev                    # dev server on :3000, proxy /api yourself
```

No Node installed? Build the frontend in a container instead:

```bash
docker run --rm -v "$PWD/frontend":/app -w /app node:20 npm ci
docker run --rm -v "$PWD/frontend":/app -w /app node:20 npm run build
```

## Testing

- **Backend** — `cd backend && uv run pytest -q` (unit + API, no network)
- **E2E** — Playwright against the real container, with `LLM_MOCK=true`:

```bash
docker compose -f test/docker-compose.test.yml up --build --exit-code-from playwright
```

Browser dependencies stay out of the production image; the E2E stack adds a Playwright
container beside it and gates on the app's healthcheck.

## Project Structure

```
finally/
├── frontend/    # Next.js static export (app/, components/, lib/)
├── backend/     # FastAPI uv project (app/market, app/chat, app/schema)
├── planning/    # Project documentation and agent contracts
├── test/        # Playwright E2E tests + docker-compose.test.yml
├── db/          # SQLite volume mount (runtime)
└── scripts/     # Start/stop helpers for macOS and Windows
```

## License

See [LICENSE](LICENSE).
