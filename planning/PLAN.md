# FinAlly — AI Trading Workstation

## Project Specification

## 1. Vision

FinAlly (Finance Ally) is a visually stunning AI-powered trading workstation that streams live market data, lets users trade a simulated portfolio, and integrates an LLM chat assistant that can analyze positions and execute trades on the user's behalf. It looks and feels like a modern Bloomberg terminal with an AI copilot.

This is the capstone project for an agentic AI coding course. It is built entirely by Coding Agents demonstrating how orchestrated AI agents can produce a production-quality full-stack application. Agents interact through files in `planning/`.

## 2. User Experience

### First Launch

The user runs a single Docker command (or a provided start script). A browser opens to `http://localhost:8000`. No login, no signup. They immediately see:

- A watchlist of tickers with live-updating prices in a grid
- $10,000 in virtual cash
- A dark, data-rich trading terminal aesthetic
- An AI chat panel ready to assist

### What the User Can Do

- **Watch prices stream** — prices flash green (uptick) or red (downtick) with subtle CSS animations that fade
- **View sparkline mini-charts** — price action beside each ticker in the watchlist, accumulated on the frontend from the SSE stream since page load (sparklines fill in progressively)
- **Click a ticker** to see a larger detailed chart in the main chart area
- **Buy and sell shares** — market orders only, instant fill at current price, no fees, no confirmation dialog
- **Monitor their portfolio** — a heatmap (treemap) showing positions sized by weight and colored by P&L, plus a P&L chart tracking total portfolio value over time
- **View a positions table** — ticker, quantity, average cost, current price, unrealized P&L, % change
- **Chat with the AI assistant** — ask about their portfolio, get analysis, and have the AI execute trades and manage the watchlist through natural language
- **Manage the watchlist** — add/remove tickers manually or via the AI chat

### Visual Design

- **Dark theme**: backgrounds around `#0d1117` or `#1a1a2e`, muted gray borders, no pure black
- **Price flash animations**: brief green/red background highlight on price change, fading over ~500ms via CSS transitions
- **Connection status indicator**: a small colored dot (green = connected, yellow = reconnecting, red = disconnected) visible in the header
- **Professional, data-dense layout**: inspired by Bloomberg/trading terminals — every pixel earns its place
- **Responsive but desktop-first**: optimized for wide screens, functional on tablet

### Color Scheme
- Accent Yellow: `#ecad0a`
- Blue Primary: `#209dd7`
- Purple Secondary: `#753991` (submit buttons)

## 3. Architecture Overview

### Single Container, Single Port

```
┌─────────────────────────────────────────────────┐
│  Docker Container (port 8000)                   │
│                                                 │
│  FastAPI (Python/uv)                            │
│  ├── /api/*          REST endpoints             │
│  ├── /api/stream/*   SSE streaming              │
│  └── /*              Static file serving         │
│                      (Next.js export)            │
│                                                 │
│  SQLite database (volume-mounted)               │
│  Background task: market data polling/sim        │
└─────────────────────────────────────────────────┘
```

- **Frontend**: Next.js with TypeScript, built as a static export (`output: 'export'`), served by FastAPI as static files
- **Backend**: FastAPI (Python), managed as a `uv` project
- **Database**: SQLite, single file at `db/finally.db`, volume-mounted for persistence
- **Real-time data**: Server-Sent Events (SSE) — simpler than WebSockets, one-way server→client push, works everywhere
- **AI integration**: LiteLLM → OpenRouter (Cerebras for fast inference), with structured outputs for trade execution
- **Market data**: Environment-variable driven — simulator by default, real data via Massive API if key provided
- **Process model**: Exactly **one** `uvicorn` worker. The price cache and the background market-data task live in process memory, so a second worker would get its own independent cache plus a second writer racing on the same SQLite file. Scale by introducing a shared cache and database, never by raising the worker count.

### Why These Choices

| Decision | Rationale |
|---|---|
| SSE over WebSockets | One-way push is all we need; simpler, no bidirectional complexity, universal browser support |
| Static Next.js export | Single origin, no CORS issues, one port, one container, simple deployment |
| SQLite over Postgres | No auth = no multi-user = no need for a database server; self-contained, zero config |
| Single Docker container | Students run one command; no docker-compose for production, no service orchestration |
| uv for Python | Fast, modern Python project management; reproducible lockfile; what students should learn |
| Market orders only | Eliminates order book, limit order logic, partial fills — dramatically simpler portfolio math |

---

## 4. Directory Structure

```
finally/
├── frontend/                 # Next.js TypeScript project (static export)
├── backend/                  # FastAPI uv project (Python)
│   └── app/schema/           # Schema definitions, seed data, init logic
├── planning/                 # Project-wide documentation for agents
│   ├── PLAN.md               # This document
│   └── ...                   # Additional agent reference docs
├── scripts/
│   ├── start_mac.sh          # Launch Docker container (macOS/Linux)
│   ├── stop_mac.sh           # Stop Docker container (macOS/Linux)
│   ├── start_windows.ps1     # Launch Docker container (Windows PowerShell)
│   ├── stop_windows.ps1      # Stop Docker container (Windows PowerShell)
│   ├── lib_portfolio_tool.{sh,ps1}      # Shared runner for the four harness scripts
│   ├── equal_weight_portfolio.{sh,ps1}  # Seed an equal-dollar-weight book
│   ├── start_random_portfolio.{sh,ps1}  # Seed a reproducible, lopsided random book
│   ├── save_session.{sh,ps1}            # Portfolio state -> sessions/NAME.json
│   └── load_session.{sh,ps1}            # sessions/NAME.json -> portfolio state
├── sessions/                 # Saved portfolio sessions (JSON, hand-editable)
├── test/                     # Playwright E2E tests + docker-compose.test.yml
├── db/                       # Volume mount target (SQLite file lives here at runtime)
│   └── .gitkeep              # Directory exists in repo; finally.db is gitignored
├── Dockerfile                # Multi-stage build (Node → Python)
├── docker-compose.yml        # Optional convenience wrapper
├── .env                      # Environment variables (gitignored, .env.example committed)
└── .gitignore
```

### Key Boundaries

- **`frontend/`** is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- **`backend/`** is a self-contained uv project with its own `pyproject.toml`. It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration. Internal structure is up to the Backend/Market Data agents.
- **`backend/app/schema/`** contains schema SQL definitions and seed logic. The backend lazily initializes the database on first request — creating tables and seeding default data if the SQLite file doesn't exist or is empty. Note this path deliberately avoids `backend/db/`: the backend is copied to `/app` in the image, so a `backend/db/` would land on `/app/db` and be **shadowed at runtime by the data volume** mounted there, leaving lazy init with no schema to apply.
- **`db/`** at the top level is the runtime volume mount point. The SQLite file (`db/finally.db`) is created here by the backend and persists across container restarts via Docker volume.
- **`planning/`** contains project-wide documentation, including this plan. All agents reference files here as the shared contract.
- **`test/`** contains Playwright E2E tests and supporting infrastructure (e.g., `docker-compose.test.yml`). Unit tests live within `frontend/` and `backend/` respectively, following each framework's conventions.
- **`scripts/`** contains start/stop scripts that wrap Docker commands.

---

## 5. Environment Variables

```bash
# Required: OpenRouter API key for LLM chat functionality
OPENROUTER_API_KEY=your-openrouter-api-key-here

# Optional: Massive (Polygon.io) API key for real market data
# If not set, the built-in market simulator is used (recommended for most users)
MASSIVE_API_KEY=

# Optional: Set to "true" for deterministic mock LLM responses (testing)
LLM_MOCK=false
```

### Behavior

- If `MASSIVE_API_KEY` is set and non-empty → the backend **probes what the key is entitled to** and resolves to `LIVE` or `ANCHORED` accordingly, degrading to `SIMULATED` if the key turns out to be unusable. It is not a binary switch — see §6 and §13 item 9
- If `MASSIVE_API_KEY` is absent or empty → backend uses the built-in market simulator (`SIMULATED`)
- There is deliberately **no env var to force a mode.** Hand-setting one would let a user select `LIVE` on a key that cannot serve it — the exact failure the probe exists to catch
- If `LLM_MOCK=true` → backend returns deterministic mock LLM responses (for E2E tests)
- If `OPENROUTER_API_KEY` is absent **and** `LLM_MOCK` is not `true` → the backend **fails fast at startup** with an error naming the missing variable. Chat is a core feature; a silently half-working app is worse than a clear message.

### How Variables Reach the Backend

The backend reads configuration from `os.environ` only — it never parses a `.env` file itself. `.env` lives in the project root, is gitignored, and is passed to the container via `--env-file .env` (or the `env_file:` key in compose). This is a single mechanism with a single source of truth; do not also mount `.env` into the image.

`.env.example` is committed with every key present and blank values, so contributors can copy it.

---

## 6. Market Data

### Two Implementations, Three Modes, One Interface

Both the simulator and the Massive client implement the same abstract interface. All downstream code (SSE streaming, price cache, frontend) is agnostic to the source.

The backend does **not** pick between them on the mere presence of `MASSIVE_API_KEY`. It probes the key's entitlement at startup and resolves to one of three modes. Full derivation in `MARKET_DATA_DESIGN.md` §1–§2; decision recorded at §13 item 9.

| Mode | When | Anchor (`open_price`) | Intraday motion |
|---|---|---|---|
| `LIVE` | Key entitled to snapshots (Starter+) | Real daily open from the API | Real, polled every ~15s |
| `ANCHORED` | Key is aggregates-only (Basic) | **Real previous close** from the API | Simulated (GBM) |
| `SIMULATED` | No key, unusable key, or SDK missing | Seed price from the table | Simulated (GBM) |

`ANCHORED` exists because a Basic key 403s on both snapshot endpoints. Treating that as "no real data" would throw away a key that can still supply genuine closing prices — so the mode keeps the real anchor and simulates only the motion on top of it. **This is the mode a Basic key lands in, and it is what `/api/health` reports as `mode`.** The frontend should surface the mode rather than claiming "live" unconditionally.

### Simulator (Default)

- Generates prices using geometric Brownian motion (GBM) with configurable drift and volatility per ticker
- Updates at ~500ms intervals
- Correlated moves across tickers (e.g., tech stocks move together)
- Occasional random "events" — sudden 2-5% moves on a ticker for drama
- Starts from realistic seed prices (e.g., AAPL ~$190, GOOGL ~$175, etc.)
- Runs as an in-process background task — no external dependencies

### Massive API (Optional)

- REST API polling (not WebSocket) — simpler, works on all tiers
- Polls for the union of all watched tickers on a configurable interval
- Basic tier (5 calls/min): rate-limited by a **sliding window**, not a token bucket — a bucket that starts full lets a sixth request through ~12s into the window, which is six inside a rolling minute against a limit of five
- Paid tiers: poll every 2-15 seconds depending on tier
- Parses REST response into the same format as the simulator
- Only a Starter+ key reaches `LIVE`. A Basic key supplies anchors only, which is what `ANCHORED` is for

### Shared Price Cache

- A single background task (simulator or Massive poller) writes to an in-memory price cache
- SSE streams read from this cache and push updates to connected clients
- This architecture supports future multi-user scenarios without changes to the data layer

**Per-ticker cache entry:**

| Field | Meaning |
|---|---|
| `price` | Latest price |
| `prev_price` | Price at the previous tick (drives the green/red flash direction) |
| `open_price` | Session anchor for daily change %. Which price this is depends on the mode: seed price under `SIMULATED`, **real previous close** under `ANCHORED`, real daily open under `LIVE` (see the mode table above). Set once when the ticker enters the cache and not overwritten by ticks; it rolls only at the 09:30 ET session boundary. |
| `ts` | ISO timestamp of the latest tick |
| `history` | Bounded ring buffer of recent `(ts, price)` points — see below |

Daily change % is `(price - open_price) / open_price`. Without `open_price` this is not computable: `prev_price` is the last *tick*, not the session open, so differencing against it yields a per-tick delta near zero rather than a daily move.

**Price history ring buffer.** Each cache entry keeps the most recent ~1,000 `(ts, price)` points in memory (roughly 8 minutes at the 500ms cadence, longer under Massive polling). This backs `GET /api/prices/{ticker}/history` so the main chart renders immediately on page load instead of starting blank and filling in over minutes. It is deliberately in-memory and bounded — history does not survive a restart, and that is acceptable for a simulated workstation.

**Tracked ticker set.** The cache tracks the **union of watchlist tickers and open-position tickers**, not the watchlist alone. A user can buy a ticker and then remove it from the watchlist; the position survives (there is no cascade delete), and if its price stopped updating the portfolio value, heatmap, and P&L chart would all silently go stale. The set is recomputed whenever the watchlist or positions change.

### SSE Streaming

- Endpoint: `GET /api/stream/prices`
- Long-lived SSE connection; client uses native `EventSource` API
- Server pushes price updates for every ticker in the tracked set (watchlist ∪ open positions) at a regular cadence (~500ms)
- Each SSE event contains ticker, price, previous price, open price, timestamp, and change direction
- Client handles reconnection automatically (EventSource has built-in retry)

---

## 7. Database

### SQLite with Lazy Initialization

The backend checks for the SQLite database on startup (or first request). If the file doesn't exist or tables are missing, it creates the schema and seeds default data. This means:

- No separate migration step
- No manual database setup
- Fresh Docker volumes start with a clean, seeded database automatically

### Schema

All tables include a `user_id` column defaulting to `"default"`. This is hardcoded for now (single-user) but enables future multi-user support without schema migration.

**users_profile** — User state (cash balance)
- `id` TEXT PRIMARY KEY (default: `"default"`)
- `cash_balance` REAL (default: `10000.0`)
- `created_at` TEXT (ISO timestamp)

**watchlist** — Tickers the user is watching
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `added_at` TEXT (ISO timestamp)
- UNIQUE constraint on `(user_id, ticker)`

**positions** — Current holdings (one row per ticker per user)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `quantity` REAL (fractional shares supported)
- `avg_cost` REAL
- `updated_at` TEXT (ISO timestamp)
- UNIQUE constraint on `(user_id, ticker)`

**trades** — Trade history (append-only log)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `side` TEXT (`"buy"` or `"sell"`)
- `quantity` REAL (fractional shares supported)
- `price` REAL
- `executed_at` TEXT (ISO timestamp)

**portfolio_snapshots** — Portfolio value over time (for P&L chart). Recorded every 30 seconds by a background task, and immediately after each trade execution.
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `total_value` REAL
- `recorded_at` TEXT (ISO timestamp)

**chat_messages** — Conversation history with LLM
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `role` TEXT (`"user"` or `"assistant"`)
- `content` TEXT
- `actions` TEXT (JSON — trades executed, watchlist changes made; null for user messages)
- `created_at` TEXT (ISO timestamp)

### Default Seed Data

- One user profile: `id="default"`, `cash_balance=10000.0`
- Ten watchlist entries: ALAB, MRVL, MU, AMD, INTC, PLTR, ANET, LRCX, AMAT, SLV

Seed entries are bare exchange symbols — no company names, no punctuation — because that is all the schema stores and all a market data provider will accept. Two earlier entries were corrected here: `INTEL` → **`INTC`** (Intel's actual symbol; `INTEL` 404s against any real provider) and `MU / Micron` → **`MU`**. This matters in practice because a non-empty `MASSIVE_API_KEY` routes seed tickers straight to the live API on first run.

---

## 8. API Endpoints

### Market Data
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/stream/prices` | SSE stream of live price updates |
| GET | `/api/prices/{ticker}/history` | Recent `(ts, price)` points from the cache ring buffer, so charts render populated on first paint |
| GET | `/api/prices/history?tickers=A,B,C` | Bulk form of the above. Seeds every watchlist sparkline in one round trip instead of N — the main chart uses the per-ticker route, the watchlist uses this one |

### Portfolio
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/portfolio` | Current positions, cash balance, total value, unrealized P&L |
| POST | `/api/portfolio/trade` | Execute a trade: `{ticker, quantity, side}` |
| GET | `/api/portfolio/history` | Portfolio value snapshots over time (for P&L chart) |
| POST | `/api/portfolio/reset` | Back to $10,000, the seed watchlist, and no history. E2E needs it — the fresh-start scenario fails on a second run against a persisted volume otherwise — and it is the escape hatch when a demo's LLM drains the account. Runs under the same trade lock as `/api/portfolio/trade`, so it can never interleave with an in-flight trade |

### Analytics
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/analytics/risk` | Volatility, expected return, Sharpe, VaR and the per-name risk decomposition for a weight vector. Read-only; empty `holdings` means the live portfolio |
| POST | `/api/analytics/rebalance` | Suggests target weights (`min_variance`, `risk_parity`, `max_sharpe`, `equal_weight`) and the ordered trades that reach them. **Suggests only — executes nothing** |
| POST | `/api/portfolio/rebalance` | Executes a suggested trade list. The whole batch runs under one hold of the trade lock, so nothing interleaves between the sells and the buys. A partial batch is a valid outcome, per §9 |

### Daily history
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/history/portfolio` | Daily USD value of the real book reconstructed from a dated CEDEAR ledger. `range=1m\|3m\|6m\|ytd\|max`. Carries `total_value` **and** `return_pct` rebased to the filtered window, so the $/% toggle is a field swap. Returns 200 with `available: false` when no ledger has been generated |
| GET | `/api/history/prices?tickers=A,B` | Bulk daily closes; an unknown symbol yields `[]` rather than failing the batch |
| GET | `/api/history/prices/{ticker}` | One ticker's daily closes — the main chart's non-LIVE ranges. 404 on an unknown symbol |
| GET | `/api/history/session` | The reconstructed book as a `POST /api/session` document, for `load_history` |

Separate from `/api/portfolio/history` and `/api/prices/{ticker}/history` on purpose: those serve
the simulated $10,000 account at 30s granularity and the intraday ring buffer, this serves a
different account at daily granularity from a committed artifact. See `PORTFOLIO_HISTORY.md` §8.

### Sessions
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/session` | Export cash, positions (with average costs) and the watchlist as one JSON document |
| POST | `/api/session` | Restore such a document, replacing the current portfolio. Exists because a load is **not** expressible through the trade API: replaying positions as market orders fills at today's price and silently rewrites every cost basis and the cash balance. Same trade lock as `/api/portfolio/reset`. Clears trades and P&L snapshots (they describe an account that no longer exists); keeps chat history. See `REBALANCE_TEST_HARNESS.md` §6 |

### Watchlist
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/watchlist` | Current watchlist tickers with latest prices |
| POST | `/api/watchlist` | Add a ticker: `{ticker}` |
| DELETE | `/api/watchlist/{ticker}` | Remove a ticker |

### Chat
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/chat` | Send a message, receive complete JSON response (message + executed actions) |
| GET | `/api/chat/history` | Prior conversation, so a page reload restores the panel instead of showing an empty chat the LLM still remembers |

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check (for Docker/deployment) |

---

## 9. LLM Integration

When writing code to make calls to LLMs, use cerebras-inference skill to use LiteLLM via OpenRouter to the `openrouter/openai/gpt-oss-120b` model with Cerebras as the inference provider. Structured Outputs should be used to interpret the results.

There is an OPENROUTER_API_KEY in the .env file in the project root.

### How It Works

When the user sends a chat message, the backend:

1. Loads the user's current portfolio context (cash, positions with P&L, watchlist with live prices, total portfolio value)
2. Loads conversation history from the `chat_messages` table — messages from the **last 30 days, capped at the 50 most recent** (selected newest-first, then re-sorted ascending for the prompt). The cap rarely binds in normal use but keeps a heavy chat session from inflating prompt size and cost without bound.
3. Constructs a prompt with a system message, portfolio context, conversation history, and the user's new message
4. Calls the LLM via LiteLLM → OpenRouter, requesting structured output, using the cerebras-inference skill
5. Parses the complete structured JSON response
6. Auto-executes any trades or watchlist changes specified in the response
7. Stores the message and executed actions in `chat_messages`
8. Returns the complete JSON response to the frontend (no token-by-token streaming — Cerebras inference is fast enough that a loading indicator is sufficient)

### Structured Output Schema

The LLM is instructed to respond with JSON matching this schema:

```json
{
  "message": "Your conversational response to the user",
  "trades": [
    {"ticker": "AAPL", "side": "buy", "quantity": 10}
  ],
  "watchlist_changes": [
    {"ticker": "PYPL", "action": "add"}
  ]
}
```

- `message` (required): The conversational text shown to the user
- `trades` (optional): Array of trades to auto-execute. Each trade goes through the same validation as manual trades (sufficient cash for buys, sufficient shares for sells)
- `watchlist_changes` (optional): Array of watchlist modifications

### Auto-Execution

Trades specified by the LLM execute automatically — no confirmation dialog. This is a deliberate design choice:
- It's a simulated environment with fake money, so the stakes are zero
- It creates an impressive, fluid demo experience
- It demonstrates agentic AI capabilities — the core theme of the course

If a trade fails validation (e.g., insufficient cash), the error is included in the chat response so the LLM can inform the user.

**Trades execute sequentially, never in parallel.** Each trade in the `trades` array is validated against the balance left by the trades before it, in array order. Validating them all against the pre-response snapshot would let two individually-affordable buys both pass and drive cash negative. A partial batch is a valid outcome: earlier trades stand, the first failure and every subsequent trade are reported back with their reason.

**Tickers outside the watchlist are auto-added.** If the LLM trades a ticker the user isn't watching, the backend adds it to the watchlist first, which pulls it into the tracked ticker set (§6) and gives it a live price. Without this the trade would have no price to fill at. The watchlist addition is reported in the response's actions alongside the trade.

### System Prompt Guidance

The LLM should be prompted as "FinAlly, an AI trading assistant" with instructions to:
- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with reasoning
- Execute trades when the user asks or agrees
- Manage the watchlist proactively
- Be concise and data-driven in responses
- Always respond with valid structured JSON

### LLM Mock Mode

When `LLM_MOCK=true`, the backend returns deterministic mock responses instead of calling OpenRouter. This enables:
- Fast, free, reproducible E2E tests
- Development without an API key
- CI/CD pipelines

---

## 10. Frontend Design

### Layout

The frontend is a single-page application with a dense, terminal-inspired layout. The specific component architecture and layout system is up to the Frontend Engineer, but the UI should include these elements:

- **Watchlist panel** — grid/table of watched tickers with: ticker symbol, current price (flashing green/red on change), daily change % (computed from the `open_price` anchor on each SSE event, not from the previous tick), and a sparkline mini-chart
- **Main chart area** — larger chart for the currently selected ticker, with at minimum price over time. Seeded from `GET /api/prices/{ticker}/history` on selection so it renders populated immediately, then extended live from the SSE stream. Clicking a ticker in the watchlist selects it here.
- **Portfolio heatmap** — treemap visualization where each rectangle is a position, sized by portfolio weight, colored by P&L (green = profit, red = loss)
- **P&L chart** — line chart showing total portfolio value over time, using data from `portfolio_snapshots`
- **Positions table** — tabular view of all positions: ticker, quantity, avg cost, current price, unrealized P&L, % change
- **Trade bar** — simple input area: ticker field, quantity field, buy button, sell button. Market orders, instant fill.
- **Analytics drawer** — opened by the **Risk & Return** and **Suggest Rebalance** buttons on the positions header. Two tabs over one selection: a risk/return scatter with the portfolio marked, weight-vs-risk-share bars, and a current-vs-target rebalance preview with the ordered trade list and an explicit Apply. See `PORTFOLIO_ANALYTICS.md`
- **Time-range strip** — `LIVE 1M 3M 6M YTD MAX` on both the P&L chart and the main chart, plus a `$`/`%` toggle on the former. `LIVE` keeps the snapshot and SSE behaviour above unchanged; the longer ranges draw daily closes from `/api/history/*`. The P&L chart opens on `MAX` when a reconstructed ledger is present, so the portfolio's evolution is what the page loads with. See `PORTFOLIO_HISTORY.md`
- **AI chat panel** — docked/collapsible sidebar. Message input, scrolling conversation history, loading indicator while waiting for LLM response. Trade executions and watchlist changes shown inline as confirmations.
- **Header** — portfolio total value (updating live), connection status indicator, cash balance

### Technical Notes

- Use `EventSource` for SSE connection to `/api/stream/prices`
- Canvas-based charting library preferred (Lightweight Charts or Recharts) for performance
- Price flash effect: on receiving a new price, briefly apply a CSS class with background color transition, then remove it
- Seed charts and sparklines from `/api/prices/{ticker}/history`, then append SSE ticks — this avoids the blank-chart-on-load problem
- Restore the chat panel from `/api/chat/history` on mount, so a refresh doesn't show an empty conversation the assistant still has context for
- All API calls go to the same origin (`/api/*`) — no CORS configuration needed
- Tailwind CSS for styling with a custom dark theme

---

## 11. Docker & Deployment

### Multi-Stage Dockerfile

```
Stage 1: Node 20 slim
  - Copy frontend/
  - npm install && npm run build (produces static export)

Stage 2: Python 3.12 slim
  - Install uv
  - Copy backend/
  - uv sync (install Python dependencies from lockfile)
  - Copy frontend build output into a static/ directory
  - Expose port 8000
  - CMD: uvicorn serving FastAPI app (single worker — see below)
```

FastAPI serves the static frontend files and all API routes on port 8000.

**Run with exactly one worker.** Do not pass `--workers N` (N > 1) or use Gunicorn with multiple uvicorn workers. Per §3, the price cache and the market-data background task are in-process: extra workers would each spin up their own cache and their own simulator/poller, so clients would see different prices depending on which worker served their SSE connection, and multiple processes would contend for writes on one SQLite file.

### Docker Volume

The SQLite database persists via a named Docker volume:

```bash
docker run -v finally-data:/app/db -p 8000:8000 --env-file .env finally
```

The `db/` directory in the project root maps to `/app/db` in the container. The backend writes `finally.db` to this path.

### Start/Stop Scripts

**`scripts/start_mac.sh`** (macOS/Linux):
- Builds the Docker image if not already built (or if `--build` flag passed)
- Runs the container with the volume mount, port mapping, and `.env` file
- Prints the URL to access the app
- Optionally opens the browser

**`scripts/stop_mac.sh`** (macOS/Linux):
- Stops and removes the running container
- Does NOT remove the volume (data persists)

**`scripts/start_windows.ps1`** / **`scripts/stop_windows.ps1`**: PowerShell equivalents for Windows.

All scripts should be idempotent — safe to run multiple times.

### Optional Cloud Deployment

The container is designed to deploy to AWS App Runner, Render, or any container platform. A Terraform configuration for App Runner may be provided in a `deploy/` directory as a stretch goal, but is not part of the core build.

---

## 12. Testing Strategy

### Unit Tests (within `frontend/` and `backend/`)

**Backend (pytest)**:
- Market data: simulator generates valid prices, GBM math is correct, Massive API response parsing works, both implementations conform to the abstract interface
- Portfolio: trade execution logic, P&L calculations, edge cases (selling more than owned, buying with insufficient cash, selling at a loss)
- LLM: structured output parsing handles all valid schemas, graceful handling of malformed responses, trade validation within chat flow
- API routes: correct status codes, response shapes, error handling

**Frontend (React Testing Library or similar)**:
- Component rendering with mock data
- Price flash animation triggers correctly on price changes
- Watchlist CRUD operations
- Portfolio display calculations
- Chat message rendering and loading state

### E2E Tests (in `test/`)

**Infrastructure**: A separate `docker-compose.test.yml` in `test/` that spins up the app container plus a Playwright container. This keeps browser dependencies out of the production image.

**Environment**: Tests run with `LLM_MOCK=true` by default for speed and determinism.

**Key Scenarios**:
- Fresh start: default watchlist appears, $10k balance shown, prices are streaming
- Add and remove a ticker from the watchlist
- Buy shares: cash decreases, position appears, portfolio updates
- Sell shares: cash increases, position updates or disappears
- Portfolio visualization: heatmap renders with correct colors, P&L chart has data points
- AI chat (mocked): send a message, receive a response, trade execution appears inline
- SSE resilience: disconnect and verify reconnection

---

## 13. Decision Record

*Doc review 2026-08-08. Every item below is **resolved** and already folded into §1–§12 above — this section exists so the reasoning survives, not as a to-do list.*

| # | Question | Decision | Landed in |
|---|---|---|---|
| 1 | Is the empty `backend/` intentional? | Yes — deleted deliberately to regenerate brand-new code. Prior design notes remain in `planning/archive/`. `CLAUDE.md` updated to drop the "completed" claim and the dead `MARKET_DATA_SUMMARY.md` pointer. | `CLAUDE.md` |
| 2 | Where does the main chart get its history? | Add a bounded ring buffer (~1,000 points) per cache entry, exposed as `GET /api/prices/{ticker}/history`. Charts seed from it, then extend live from SSE. | §6, §8, §10 |
| 3 | What anchors "daily change %"? | Add `open_price` to each cache entry. Change % is `(price - open_price) / open_price`. *Which* price the anchor is turned out to depend on the mode — see item 9, which supersedes this row's "daily open from the API for Massive". | §6, §10 |
| 4 | Is chat history restored on reload? | Yes — `GET /api/chat/history`, called on mount. | §8, §10 |
| 5 | How are multiple LLM trades validated? | Always sequentially, each against the balance left by its predecessors. Partial batches are valid; failures report the reason. | §9 |
| 6 | Can the LLM trade an unwatched ticker? | Auto-add it to the watchlist first, which pulls it into the tracked set and gives it a live price. Reported in the response actions. | §6, §9 |
| 7 | Missing `OPENROUTER_API_KEY` with `LLM_MOCK` unset? | Fail fast at startup with an error naming the variable. | §5 |
| 8 | How much chat history enters the prompt? | Last 30 days, capped at the 50 most recent messages. | §9 |
| 9 | Does a populated `MASSIVE_API_KEY` really mean "real data"? | **No — the market data source resolves to one of three modes, not two.** Live probing showed §5's binary is unachievable: this project's Basic-tier key 403s on both snapshot endpoints, so "key set → real prices" would have failed closed to the simulator and silently thrown the key away. The backend now probes the key's entitlement at startup and picks: **`LIVE`** (Starter+, real snapshots polled every 15s), **`ANCHORED`** (Basic — real previous closes as the session anchor, simulated intraday motion on top), or **`SIMULATED`** (no key, unusable key, or SDK missing). The probe never raises; every failure degrades one step down. Mode is **deliberately not an env var** — a hand-set mode would let a user select LIVE on a key that cannot serve it, which is the exact failure the probe exists to prevent. Full derivation in `MARKET_DATA_DESIGN.md` §1–§2. | §5, §6 |

### Inconsistencies corrected

1. **`backend/db/` shadowed by the data volume.** The backend is copied to `/app`, so `backend/db/` would land on `/app/db` — exactly where the SQLite volume mounts — hiding the schema files from lazy init at runtime. Schema code moved to **`backend/app/schema/`**; `/app/db` is now purely a data volume. (§4)

2. **Two malformed seed tickers.** `INTEL` → **`INTC`** (Intel's real symbol; `INTEL` 404s against any provider) and `MU / Micron` → **`MU`**. Seed entries are bare exchange symbols only. (§7)

3. **The real API is the local default, not the simulator.** The project-root `.env` carries a populated `MASSIVE_API_KEY`, so this machine hits Massive on first run rather than the simulator §6 calls "recommended for most users." Fixing item 2 above is what makes that safe. Verified 2026-08-15: that key is Basic-tier, so it resolves to **`ANCHORED`** — real previous closes, simulated intraday motion — not `LIVE`. To develop fully offline, blank the key locally.

4. **Stale prices on de-watchlisted positions.** The cache now tracks the **union of watchlist and open-position tickers**. Previously, buying a ticker and then removing it from the watchlist left the position with a frozen price, silently corrupting portfolio value, the heatmap, and the P&L chart. (§6)

5. **`.gitignore` was Python-only.** Added `node_modules/`, `.next/`, `out/`, and the frontend build artifacts a Next.js `frontend/` will produce, plus `db/finally.db` — which §4 already claimed was ignored but wasn't. (`.gitignore`)

6. **Single-worker constraint was implicit.** The in-process price cache and background task only behave correctly under one uvicorn worker; multiple workers would each hold a private cache and race on SQLite writes. Now stated explicitly. (§3, §11)

7. **Two competing env-delivery mechanisms.** §5 said the backend reads `.env` from the project root while §11 passed `--env-file` — and inside the container no project-root `.env` exists. Settled on `--env-file` plus `os.environ`; the backend never parses `.env` itself. (§5)

### Still open

- **Charting library.** §10 still offers "Lightweight Charts or Recharts." The UI needs line charts, sparklines, *and* a treemap; Lightweight Charts has no treemap, so that branch quietly costs a second charting dependency. Left as the Frontend Engineer's call.
