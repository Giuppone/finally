# PLAN.md — Review

**Reviewed:** `planning/PLAN.md` (§1–§13)
**Date:** 2026-08-09
**Reviewer:** Coding agent (plan review pass)
**Method:** Read PLAN.md in full; cross-checked its claims against the actual repo state (`.env`, `.env.example`, `.gitignore`, `CLAUDE.md`, `README.md`, tracked files) and against the superseded design notes in `planning/archive/`.

---

## 0. Verdict

The plan is **well above the bar for an implementable spec**. Scope is honestly bounded, the technology choices are justified rather than asserted, and §13 shows a real review loop already happened. An agent could start building from this today.

The issues below are therefore not "this plan is wrong" — they are **the places where two agents working in parallel will produce incompatible code**, because the doc reads as settled where it is actually silent. The single highest-value change is §A1: §8 is presented as the frontend/backend contract but contains no payload shapes at all.

Findings are tagged:

- **[BLOCKER]** — will cause rework or a broken build if not resolved before coding starts
- **[GAP]** — an implementing agent must guess; guesses will diverge across agents
- **[RISK]** — feasible but likely to bite during the demo or in CI
- **[NIT]** — correctness of the document itself

---

## A. Blockers — resolve before any agent writes code

### A1. §8 is called a contract but specifies no payloads [BLOCKER]

§4 states "All agents reference files here as the shared contract," and §8 is the only place API surface is defined. But §8 gives *path + one sentence of prose* per endpoint and **zero request/response schemas**. Nothing in the doc fixes:

- field naming (`prev_price` / `previous_price` / `previousPrice` — the archive uses `previous_price`, §6's cache table uses `prev_price`, and the frontend is TypeScript where camelCase is idiomatic)
- error response shape and status codes (what does a rejected trade return — 400 with `{detail}`? 200 with `{ok: false}`?)
- whether `/api/portfolio` nests positions or returns a flat list
- query parameters (see A2)

The Frontend and Backend agents are explicitly allowed to choose their own internals (§4), which is correct — but that only works if the seam between them is nailed down. Right now the seam is prose.

**Recommendation:** Before the build starts, produce `planning/API.md` with a concrete JSON example of every request and response in §8, including the error shape, and declare it the authoritative contract. Pick **snake_case on the wire** (it matches the DB and Python; the frontend maps once at its fetch layer). Have the backend agent generate `frontend/src/types/api.ts` from the FastAPI OpenAPI schema so drift is caught at compile time rather than at runtime.

### A2. The SSE event format is undefined — this is the highest-traffic interface in the app [BLOCKER]

§6 says only: "Each SSE event contains ticker, price, previous price, open price, timestamp, and change direction." That leaves four decisions that the frontend cannot make unilaterally:

| Undecided | Options | Recommendation |
|---|---|---|
| One event per ticker, or one event carrying all tickers? | N events @500ms vs 1 event @500ms | **One event containing all tracked tickers.** ~30 tickers is a small payload and it gives the client a consistent snapshot. (The archived `stream.py` sketch did this — a dict keyed by ticker.) |
| Named event or default `message`? | `event: prices` vs bare `data:` | **Bare `data:`**, so the client uses `onmessage` with no `addEventListener` wiring. |
| Is a `retry:` directive sent? | — | **Yes, `retry: 1000`.** §2 promises a "reconnecting" state; EventSource's default backoff is browser-dependent. |
| Are unchanged prices re-sent every 500ms? | always vs only-on-change | See A3. |

Without a decision here the frontend agent writes an `EventSource` handler against an imagined shape and it will not match.

### A3. Under Massive, the 500ms SSE cadence will produce 29 out of 30 "flat" events — and the flash logic breaks [BLOCKER]

§6 pins the stream at "~500ms" but pins the Massive poll at 15s (free tier). So for 14.5 seconds out of every 15, the SSE stream re-broadcasts an unchanged price. Two consequences:

1. **`prev_price` becomes meaningless.** If `prev_price` is recomputed per *broadcast*, it equals `price` for 29 of 30 events and the green/red flash — a headline feature in §2 — effectively never fires. If it is instead updated only per *cache write*, the same non-flat event repeats 30 times and a naive frontend flashes the same tick 30 times in a row.
2. Bandwidth is wasted re-sending identical data.

The archived implementation solved this with a **cache version counter**: the SSE generator tracks the last version it sent and yields only when the cache has actually changed. PLAN.md dropped that detail.

**Recommendation:** State explicitly that the stream emits **only when the price cache changes**, with a keepalive comment (`: ping\n\n`) every ~15s to hold the connection open through proxies. Define `prev_price` as *the price at the previous cache write*, never per broadcast. This makes the simulator and Massive paths behave identically from the frontend's point of view, which is the whole premise of §6's "one interface."

### A4. §2 and §10 give contradictory instructions for sparklines [BLOCKER]

This is a §13 decision that did not fully land.

- §2 (line 25): sparklines are "**accumulated on the frontend from the SSE stream since page load** (sparklines fill in progressively)"
- §10 (line 399): "**Seed charts and sparklines from `/api/prices/{ticker}/history`**, then append SSE ticks — this avoids the blank-chart-on-load problem"

§13 decision #2 records the ring buffer as landing in "§6, §8, §10" — §2 was never updated. A frontend agent reading top-down implements the progressive-fill version and considers itself done.

**Recommendation:** Delete the parenthetical in §2 and align it with §10. Also note the practical consequence §10 skips: seeding 10 sparklines means 10 parallel calls to `/api/prices/{ticker}/history` on mount. Either accept that, or add a bulk form — `GET /api/prices/history?tickers=A,B,C&limit=60` — which is the better answer, since sparklines need ~60 points, not 1,000.

### A5. The simulator's seed prices do not cover any of the ten default tickers [BLOCKER]

§6 says "Starts from realistic seed prices (e.g., **AAPL ~$190, GOOGL ~$175**, etc.)" — but §7's seed watchlist is **ALAB, MRVL, MU, AMD, INTC, PLTR, ANET, LRCX, AMAT, SLV**. The overlap is zero. The archived `SEED_PRICES` table (`planning/archive/MARKET_SIMULATOR.md`) covers exactly the AAPL/GOOGL/MSFT/TSLA/NVDA/META/JPM/V/NFLX/AMZN list — the *old* watchlist, still visible in `bck.txt`.

If an agent implements §6 by following the archive, **all ten default tickers fall through to the `random.uniform(50, 300)` fallback**. Consequences:

- SLV (a silver ETF, really ~$30) may open at $280; prices are absurd and differ on every restart
- `open_price` is a random number, so §6's daily-change anchor is anchored to noise
- The correlation groups in the archive are also keyed to the old tickers, so **all ten default tickers land in the 0.3 cross-group bucket** — the "tech stocks move together" feature in §6 silently does nothing, on a watchlist that is almost entirely semiconductors and would in reality be the most tightly correlated basket on the board

**Recommendation:** Put a seed-price and volatility table for the *actual* ten tickers into the plan (or into a `planning/` reference doc), plus a semis correlation group (ALAB, MRVL, MU, AMD, INTC, ANET, LRCX, AMAT ~0.7) and SLV as an uncorrelated low-vol outlier — a commodity ETF genuinely does not track semis, and having one ticker that moves independently makes the heatmap more interesting, not less.

### A6. Named volume vs bind mount — §4 and §11 disagree [BLOCKER]

§11's command uses a **named volume**: `docker run -v finally-data:/app/db`. But the very next sentence says "The `db/` directory in the project root maps to `/app/db`" — that describes a **bind mount** (`-v "$(pwd)/db:/app/db"`), and §4 lists `db/` as the "Volume mount target" with a committed `.gitkeep`.

These are different things with different behavior. With the named volume, the project-root `db/` directory is never touched, the `.gitkeep` is pointless, and the developer cannot open the SQLite file with a local tool. The plan's own §13 pass did not catch this.

**Recommendation:** Choose the **bind mount** (`./db:/app/db`). It matches §4, makes the DB inspectable, and makes "delete `db/finally.db` to reset" a one-liner for students — which is worth a lot in a teaching project. Then fix §11's command and note that `.gitignore` already covers `db/*.db` (verified — lines 221-226, including the `!db/.gitkeep` negation). Whichever way it goes, one of the two passages must change.

**Also:** `db/` does not exist in the repo yet. Neither do `scripts/`, `test/`, or `frontend/`; `backend/` exists but is empty. That is expected for a pre-build repo, but the `db/.gitkeep` in §4 needs to actually be created or the first bind-mount run will have Docker create a root-owned directory.

---

## B. Gaps — an implementing agent has to guess

### B1. Ticker normalization is never specified [GAP]

Nothing in the doc says tickers are uppercased. SQLite's default collation is case-sensitive, so `UNIQUE(user_id, ticker)` treats `aapl` and `AAPL` as distinct. §10's trade bar has a free-text ticker field and §9 lets an LLM emit arbitrary strings. Result: duplicate watchlist rows, two cache entries, two positions in the same stock, and a heatmap that shows one holding twice.

**Recommendation:** Normalize to `ticker.strip().upper()` at every API boundary, and additionally declare the column `COLLATE NOCASE` as a backstop. Also state the accepted pattern (`^[A-Z]{1,5}$`) so `POST /api/watchlist` can reject junk with a clear 400 rather than creating a dead row.

### B2. What happens when an unknown/invalid ticker is added? [GAP]

Simulator mode will happily invent a price for `ASDF`. Massive mode returns nothing for it, so the row sits in the watchlist forever with no price, no sparkline, and — if the user then trades it — no fill price. §9's auto-add path makes this reachable directly from LLM output.

**Recommendation:** On add, do a **synchronous single-ticker validation fetch** in Massive mode and reject with 400 if the symbol is unknown. Define the UI's empty state for a ticker with no price yet (`—` rather than `$0.00`; `$0.00` will render as a -100% daily change).

### B3. A newly added ticker has no price for up to 15 seconds in Massive mode [GAP]

§9 auto-adds an unwatched ticker "which pulls it into the tracked ticker set and gives it a live price" — but under Massive the next poll may be 15s away, so the very next step (execute the trade) has **no price to fill at**. The plan asserts the fix works without specifying the mechanism.

**Recommendation:** Watchlist-add must **fetch that ticker immediately** (single-ticker snapshot) and populate the cache before returning, rather than waiting for the poll loop. Same for the simulator (seed and emit one tick immediately). State this in §6 — it is what actually makes §9's auto-add claim true.

### B4. `open_price` never rolls over to a new session [GAP / correctness]

§6: `open_price` is "Set once when the ticker enters the cache and not overwritten by ticks." For a container left running for a week, "daily change %" becomes week-to-date change. For a ticker added on Thursday, it is change-since-Thursday. Two rows in the same watchlist then display percentages measured against different anchors, side by side, both labeled the same way.

Conversely, on restart every `open_price` resets to the current price, so every ticker shows 0.00% — including ones that are genuinely up 3% on the day.

**Recommendation:** Decide one of:
- **(a)** Re-anchor `open_price` at a defined session boundary (09:30 ET), and for Massive take `day.open` fresh from each poll rather than freezing the first one. Correct, slightly more code.
- **(b)** Rename the metric in the UI to "change since session start" and accept the semantics.

(a) is right for Massive mode; (b) is defensible for a simulator-only demo. Silence is the one option that produces a visibly wrong number.

### B5. §6 and the archive disagree on what anchors daily change under Massive [GAP]

§6 says `open_price` is "the daily open from the API." But market convention — and Massive's own `day.change_percent` field — computes daily change against **`day.previous_close`**, not the open. The archived response sample makes this explicit: `open: 129.61`, `previous_close: 129.61`, `close: 125.07`, `change_percent: -3.50`, which is `(125.07 - 129.61) / 129.61` — previous close, not open.

So FinAlly will display a daily change % that disagrees with every other finance site for the same ticker. There is also a divide-by-zero: during pre-market, `day.open` is 0 until the first trade prints.

**Recommendation:** Use `day.previous_close` as the anchor in Massive mode (falling back to `day.open`, then to the first observed price), and keep the field name `open_price` or rename it `anchor_price`. Guard against a zero or missing anchor before dividing.

### B6. The LLM's message and the actual trade outcome can contradict each other [GAP / logic]

§9's sequence is: (4) call LLM → (5) parse → (6) execute trades → (8) return. §13 decision #5 correctly settled that trades execute sequentially and a partial batch is valid. But then §9 says "If a trade fails validation, the error is included in the chat response **so the LLM can inform the user**" — and the LLM has already finished responding. Its `message` field, written before execution, might say "Done — I bought 10 NVDA, 5 AMD and 20 INTC" while only the first two actually filled.

**Recommendation:** Pick one:
- **(a)** Render execution results as **authoritative structured chips** in the chat panel, visually distinct from the LLM prose, and prompt the model never to narrate outcomes in the past tense. Cheap, no extra latency. **Preferred.**
- **(b)** Make a second LLM call with the execution results when any trade fails, and return that message instead. Accurate, but doubles latency exactly when something has gone wrong.

Whichever is chosen, §9 step 8 needs to say so, and the `actions` JSON needs a per-trade `status` + `reason` field.

### B7. `chat_messages.actions` JSON has no schema [GAP]

§7 describes the column as "JSON — trades executed, watchlist changes made" and §10 requires the frontend to render those inline on history restore. There is no shape. Given B6, it needs at minimum: `{trades: [{ticker, side, quantity, fill_price, status: "filled"|"rejected", reason?}], watchlist_changes: [{ticker, action, status}]}`.

### B8. `LLM_MOCK=true` behavior is undefined [GAP]

§9 promises "deterministic mock responses" and §12 requires an E2E scenario where "trade execution appears inline" — which means the mock **must** be able to emit a trade. But nothing says what the mock returns or how a test steers it.

**Recommendation:** Specify keyword-triggered fixtures, e.g. a message containing "buy" returns a canned `trades` array for a fixed ticker/quantity; anything else returns a canned analysis message with no actions. Put the fixture table in the plan so the backend and E2E agents build against the same thing.

### B9. There is no reset path, and E2E tests need one [GAP]

§12's first E2E scenario is "Fresh start: default watchlist appears, **$10k balance shown**." But the DB persists in a volume, and the buy/sell scenarios mutate it. The second run of the suite starts with a spent balance and a mutated watchlist, and the fresh-start test fails. Nothing in §8 can reset state.

**Recommendation:** Both of:
- `docker-compose.test.yml` uses a **throwaway anonymous volume** (or `tmpfs`) so every run starts clean — state this in §12.
- Add `POST /api/portfolio/reset` to §8. It is ~10 lines, it makes the demo re-runnable in front of an audience, and it is the natural escape hatch when a student's LLM drains the account.

### B10. Price-dependent E2E assertions will be flaky [RISK / GAP]

Prices move every 500ms. "Buy shares: cash decreases" is safe; "cash is exactly $9,050" is not, because the fill price differs between the click and the assertion.

**Recommendation:** Add a `SIM_SEED` env var that seeds the simulator's RNG for reproducible paths, and instruct the test agent to assert on *direction and invariants* (cash decreased; cash + market value ≈ prior total within tolerance) rather than exact figures.

### B11. Position lifecycle on a full sell is unspecified [GAP]

§12 says a position "updates or disappears" — that "or" is the entire specification. Does the row get deleted at zero quantity, or kept with `quantity = 0`? It matters for the positions table, the heatmap (a zero-weight rectangle), and the tracked ticker set (§6 keeps open-position tickers alive — a zero-quantity row would pin a ticker in the cache forever).

Related: `quantity` is REAL and fractional shares are supported, so selling "all" of a position accumulated over several buys can leave `4.4e-16` shares behind — a phantom position that renders in the table and never goes away.

**Recommendation:** Delete the row when `quantity < 1e-9`, and say so. Also state that `avg_cost` is unchanged by sells (only buys re-average), since that is the convention but is not written down anywhere.

### B12. Trade validation rules are incomplete [GAP]

§9 covers sufficient cash and sufficient shares. Not covered: `quantity <= 0`, non-numeric/NaN quantity, absurd quantities (`1e12`), and whether a buy that costs *exactly* the cash balance is allowed (floating-point equality). The LLM can emit any of these — and unlike the manual trade bar, it is not constrained by an `<input type="number" min>`.

**Recommendation:** Specify: `quantity > 0`, finite, rounded to 6 decimals; reject with a structured reason. These same rules must be applied on the LLM path (§9 says "the same validation as manual trades" — good, just make sure the shared rule set is written down).

### B13. Concurrency around trade execution is unaddressed [GAP]

Single worker (§3) does *not* mean serialized: FastAPI's async handlers interleave at every `await`. A manual trade and an LLM-driven trade batch can interleave between "read cash balance" and "write cash balance," losing an update.

**Recommendation:** Guard trade execution + snapshot write with a single `asyncio.Lock`, and do the read-validate-write in one SQLite transaction. One sentence in §7 or §9.

### B14. Timestamp format is inconsistent across the doc and the archive [GAP]

§6's cache table says `ts` is an "ISO timestamp." The archived `PriceUpdate` uses `float` Unix seconds. Massive returns Unix **milliseconds**. The DB stores ISO TEXT. Charting libraries generally want epoch ms.

**Recommendation:** Declare one wire convention: **ISO 8601 UTC with `Z`** for all REST/DB fields (string sorting then matches chronological sorting, which the `ORDER BY recorded_at` queries depend on), and note that the market layer converts provider epochs at the boundary. If the charting library prefers epoch ms, convert in the frontend, not in the API.

### B15. Heatmap color mapping is undefined [GAP]

§10 says "colored by P&L (green = profit, red = loss)" and §2's palette gives yellow/blue/purple but **no green or red hex values**, and no scale. Is a +0.5% position the same green as a +40% one? Where is the scale clipped? What color is a position at exactly 0.00%?

**Recommendation:** Specify a diverging scale with explicit endpoints — e.g. clip at ±10% unrealized P&L, neutral gray at 0 — and add the green/red hex pair to §2's palette so the flash animation, the positions table, and the treemap all use the same two colors. (An accessibility note is worth one line here: red/green alone is the classic failure case, so keep the sign in the text label too.)

### B16. `/api/portfolio` valuation before the first tick [GAP]

Immediately after a restart, positions exist in SQLite but the cache is empty. What price values the portfolio — 0? That would render a -100% P&L and a garbage snapshot row, which then permanently corrupts the P&L chart's history.

**Recommendation:** Fall back to `avg_cost` when no cached price exists, and **skip the 30s snapshot write entirely** until every position ticker has a live price.

---

## C. Feasibility risks

### C1. A blocking LLM call will freeze the entire price stream [RISK — highest-impact]

This one follows directly from the architecture and deserves prominence. §3 mandates a **single uvicorn worker**, and everything — the SSE generator, the market-data loop, the trade endpoints, the LLM call — shares one event loop. If the LiteLLM call is made synchronously (`litellm.completion`), it blocks that loop for the whole round trip. During that window: **no price ticks reach any client, every SSE connection stalls, and the connection indicator may flip to reconnecting** — precisely while the user is watching the AI do something impressive.

The same applies to the `massive` SDK, which is a synchronous HTTP client (the archive correctly wraps it in `asyncio.to_thread`; PLAN.md never mentions this), and to SQLite writes under `sqlite3`.

**Recommendation:** Add to §9: use `litellm.acompletion` (or wrap in `asyncio.to_thread`) — never a blocking call in a request handler. Add to §6: the Massive client is synchronous and **must** be called via `asyncio.to_thread`. Consider `aiosqlite` or a thread-pooled DB layer. This is one sentence in the plan and a class of extremely confusing bugs avoided.

### C2. In Massive mode the app looks dead outside US market hours [RISK]

Verified from the repo: `.env` has a **non-empty `MASSIVE_API_KEY` (32 chars)**, so per §5 this machine hits the live API on first run — §13 item 3 is accurate. The practical consequence deserves more weight than it currently gets: outside 09:30–16:00 ET, `last_trade.price` is frozen. No flashing, flat sparklines, a motionless heatmap. Every headline feature in §2 is invisible.

For a developer in Argentina (UTC-3), the US session is roughly 10:30–17:00 local, so evening and weekend development and demos land squarely in the dead zone.

**Recommendation:** State plainly in §5 or §6: **blank `MASSIVE_API_KEY` for development and for any live demo**; the simulator is the demo path, and the Massive client is the "it also works against real data" proof. Consider having the backend log a clear line at startup — `market data: SIMULATOR` / `market data: MASSIVE (live)` — so it is never a mystery which one is running.

### C3. `portfolio_snapshots` grows without bound and is returned in full [RISK]

Every 30s = 2,880 rows/day ≈ 1.05M rows/year, in a volume that persists across restarts, plus one row per trade. `GET /api/portfolio/history` has no pagination or range parameter, so the P&L chart payload grows forever and the chart eventually plots a million points.

**Recommendation:** Add `?since=` and `?limit=` to `/api/portfolio/history` with a sane default (e.g. last 24h, downsampled to ~500 points), and either a retention sweep or coarser snapshots beyond 24h. Also: skip snapshot writes when nothing has changed (no positions, no trades since the last snapshot) so an idle container does not accumulate 2,880 identical rows a day.

### C4. `uv sync` will fail without explicit package discovery [RISK — known landmine]

The archived code review (`planning/archive/MARKET_DATA_REVIEW.md`, §3.1) records that the previous backend's `pyproject.toml` was missing hatchling's package config and `uv sync` failed outright with `ValueError: Unable to determine which files to ship inside the wheel` — blocking both local setup and the Docker build. Since `backend/` is being rebuilt from scratch, the new agent will hit the identical wall.

**Recommendation:** Note it in §11 so it is prevented rather than rediscovered:

```toml
[tool.hatch.build.targets.wheel]
packages = ["app"]
```

### C5. Static-export routing details will cost an hour if unstated [RISK]

Mounting `StaticFiles(..., html=True)` at `/` **before** registering the API routers makes the mount swallow `/api/*`. Next.js `output: 'export'` also disables `next/image` optimization (needs `images: { unoptimized: true }`), API routes, and server components — worth one line so the frontend agent does not build against features that vanish at export time.

### C6. Fail-fast plus a missing key means a container that exits before the user reads why [RISK]

§5's fail-fast is the right call, but the failure mode is a container that starts and immediately dies. A student who copies `.env.example` (which ships with a blank `OPENROUTER_API_KEY` — verified) and runs `start_windows.ps1` sees a flash of text and an exited container.

**Recommendation:** Have the start scripts pre-flight-check that `.env` exists and `OPENROUTER_API_KEY` is non-empty (or `LLM_MOCK=true`), and print a clear remediation message before invoking Docker. §11 already asks for idempotent scripts; add "and validate the environment first."

---

## D. Document-level corrections

### D1. Recharts is not a canvas library [NIT]

§10: "Canvas-based charting library preferred (Lightweight Charts or Recharts)." Recharts renders **SVG** (it is a D3 wrapper). The parenthetical contradicts the requirement it is attached to.

### D2. The "still open" charting question should be closed [NIT]

§13 leaves the library choice to the Frontend Engineer while noting Lightweight Charts has no treemap. Leaving it open guarantees the agent re-derives the same analysis. The requirements are: main line chart, ~10 sparklines, one treemap, one P&L line chart, ~10-30 series updating at 2Hz.

**Recommendation: use Recharts alone.** It is the only one of the two that covers all four (`<LineChart>`, tiny `<LineChart>` for sparklines, `<Treemap>`), so it is a single dependency; SVG performance is entirely adequate at ~10 tickers and ~300 rendered points per chart if the frontend caps the retained window and throttles renders (batch SSE updates into a single state commit per animation frame rather than per event). Choose Lightweight Charts only if the main chart later needs real candlesticks and crosshairs — and then accept the second dependency for the treemap.

### D3. §4 and §7 disagree on when the DB initializes [NIT]

§4: "lazily initializes the database **on first request**." §7: "checks for the SQLite database **on startup (or first request)**."

This is actually decided for you by an ordering dependency the plan does not mention: the market-data source needs the watchlist to know which tickers to track (§6), and it starts at app startup — so the DB **must** be initialized during the lifespan startup hook, before the market task starts. Say that, and drop "or first request." It also sidesteps the two-concurrent-first-requests race that lazy init would otherwise need a lock for.

### D4. The cache-eviction rule is missing from §6 [NIT]

§6 says the tracked set "is recomputed whenever the watchlist or positions change" but never says what happens to a cache entry when a ticker *leaves* the set. Dropping it also drops its ring buffer and its `open_price`, so re-adding the ticker restarts the daily change at 0.00%. Note the intended behavior (evicting is fine — just state it).

### D5. The archive is stale relative to PLAN.md and will mislead [NIT — worth fixing]

`CLAUDE.md` tells agents to consult `planning/archive/` "when required," but those documents predate §13 and **contradict the current plan** in at least three ways:

- `MARKET_INTERFACE.md`'s `PriceUpdate` has **no `open_price`** and the `PriceCache` has **no history buffer** — §13 decisions #2 and #3 are absent
- its `remove_ticker()` calls `self._cache.remove(ticker)` unconditionally, which is exactly the stale-position bug §13 item 4 fixed
- `MARKET_SIMULATOR.md`'s seed prices and correlation groups are keyed to the superseded watchlist (see A5)

An agent that copies those sketches will faithfully reintroduce bugs the review already removed.

**Recommendation:** Add a short banner at the top of each archive file: *"Superseded by planning/PLAN.md §6. Retained for the GBM math and the Massive response shapes only; the interface, cache fields, seed prices, and ticker removal behavior are all out of date."* The GBM formulas and the Massive API response documentation remain genuinely useful — it is the interface sketches that are dangerous.

### D6. Small omissions worth one line each [NIT]

- **No realized P&L anywhere.** It is implicitly folded into cash, which is fine, but §2 promises the user can "monitor their portfolio" and there is no total-return figure. Storing the $10,000 starting cash as a constant makes `total_value - 10000` a free headline metric — high value for one line of code.
- **`/api/health` returns what?** For a compose `depends_on: service_healthy` gate (§12), it should report whether the DB is initialized and whether the market task is alive, not just `{"status":"ok"}`.
- **No CI.** `.github/workflows/` contains only the two Claude workflows — nothing runs pytest, the frontend tests, or the E2E suite. §12 defines a good test strategy with no automation to enforce it. A single workflow running backend unit tests plus a Docker build would catch C4-class breakage on every PR.
- **`docker-compose.yml`** is listed in §4 as an "optional convenience wrapper" but is never specified. Either describe it or drop it from the tree.

---

## E. Suggested sequencing

The plan describes *what* to build but not *in what order*, which matters when multiple agents work in parallel against a shared contract.

1. **Freeze the contract.** Resolve A1–A6, write `planning/API.md` (payloads, SSE event shape, error format), add the seed-price/correlation table. Nothing else starts until this exists.
2. **Backend core.** uv project (with the C4 hatch config), schema + lazy init at startup, market data layer behind the §6 interface, SSE, price cache with ring buffer. Unit tests alongside.
3. **Backend API + LLM.** Portfolio, watchlist, chat, health. `LLM_MOCK` fixtures land here so the frontend and E2E agents are unblocked without an API key.
4. **Frontend**, in parallel with 3 once the contract is frozen — against the mock backend.
5. **Docker + scripts**, then E2E last, since it needs the full container.

Steps 2 and 4 are the parallelizable pair; both depend entirely on step 1 being right.

---

## F. What the plan gets right

Worth stating explicitly, since the bulk of this document is criticism:

- **§3's rationale table and the single-worker constraint.** Explaining *why* SSE over WebSockets and *why* one worker — including the specific failure mode — is what stops a future agent from "helpfully" adding `--workers 4`.
- **§13 as a decision record rather than a to-do list.** Preserving the reasoning behind resolved questions is genuinely good practice, and most of the items it resolves (the `backend/db/` volume-shadowing trap, the tracked-set union, sequential trade validation) are subtle bugs caught before a line of code was written.
- **The tracked-set union rule (§6).** Easy to miss, silently corrupts every portfolio number if missed.
- **Sequential trade validation (§9).** Correct, and the reasoning about two individually-affordable buys is exactly right.
- **Honest scope boundaries.** Market orders only, no auth, no confirmation dialogs, in-memory history that does not survive restart — each is stated *with* its justification, which is what makes the small surface area feel deliberate rather than unfinished.

---

## Appendix: repo state as reviewed

| Path | State |
|---|---|
| `backend/` | exists, **empty** (expected per CLAUDE.md) |
| `frontend/`, `scripts/`, `test/`, `db/`, `deploy/` | **do not exist** |
| `Dockerfile`, `docker-compose.yml` | **do not exist** |
| `.env` | present; `OPENROUTER_API_KEY` non-empty, `MASSIVE_API_KEY` **non-empty** (live API is the local default), `LLM_MOCK=false` |
| `.env.example` | present, all three keys, blank values — matches §5 |
| `.gitignore` | 232 lines; `node_modules/`, `.next/`, `out/`, `db/*.db` + `!db/.gitkeep` all present — §13 item 5 is done |
| `planning/archive/` | 5 documents, all predating §13 (see D5) |
| `.github/workflows/` | 2 Claude workflows only; no test/build CI |
