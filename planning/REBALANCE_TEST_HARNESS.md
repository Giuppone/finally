# Rebalance Test Harness — seed scripts and portfolio sessions

**Status:** **implemented 2026-08-16.** This doc now describes what exists; the reasoning is kept
because it is the part worth re-reading.
**Purpose:** put the portfolio into a *reproducible, deliberately unbalanced* state so the
Suggest Rebalance button (`PORTFOLIO_ANALYTICS.md`) has something meaningful to fix, and so an E2E test
can assert on the result instead of on whatever the account happened to hold. Save/load makes any such
state repeatable across restarts and shareable as a file.

---

## 1. Deliverables

| Path | Role |
|---|---|
| `backend/scripts/portfolio_tool.py` | **all the logic** — allocation, HTTP, sessions, reporting |
| `scripts/lib_portfolio_tool.sh` / `.ps1` | shared runner: finds a Python, falls back to `docker exec` |
| `scripts/equal_weight_portfolio.sh` / `.ps1` | wrapper → `portfolio_tool.py equal` |
| `scripts/start_random_portfolio.sh` / `.ps1` | wrapper → `portfolio_tool.py random` |
| `scripts/save_session.sh` / `.ps1` | wrapper → `portfolio_tool.py save` (exact JSON) |
| `scripts/load_session.sh` / `.ps1` | wrapper → `portfolio_tool.py load` (exact JSON) |
| `scripts/import_broker.sh` / `.ps1` | wrapper → `portfolio_tool.py broker` (broker export → weights list) |
| `scripts/save_list.sh` / `.ps1` | wrapper → `portfolio_tool.py dump` (editable list) |
| `scripts/load_list.sh` / `.ps1` | wrapper → `portfolio_tool.py build` (editable list) |
| `scripts/import_broker_with_dates.sh` / `.ps1` | wrapper → `portfolio_tool.py ledger` (dated export → `backend/calibration/ledger.json`) |
| `scripts/load_history.sh` / `.ps1` | wrapper → `portfolio_tool.py load_history` (reconstructed real book → live portfolio) |
| `suggested/` | where holdings lists live |
| `backend/app/routes.py` | `GET /api/session`, `POST /api/session` (§6) |
| `backend/app/db.py` | `import_session()` — the transactional restore |
| `backend/tests/test_portfolio_tool.py` | 16 tests: the pure allocation functions, plus the --dry-run write guard |
| `backend/tests/test_trading_routes.py` | 13 added tests: session export/import |
| `sessions/` | where saved sessions land; **not gitignored** — a session file is a legitimate test fixture |

Naming notes:

- The request spelled it `start_ramdom_portfolio.sh`. Shipped as **`start_random_portfolio.sh`** — a
  typo in a filename is a permanent one.
- One tool file rather than the two the plan first sketched (`seed_portfolio.py` + a session script):
  they share the HTTP client, the health wait and the reporting table, and `scripts/` is not a package,
  so a shared module between two sibling scripts needs import gymnastics that a subcommand does not.
- The asymmetric prefixes (`start_` on one, not the other) are as requested and kept.

**Both shells ship**, matching the existing `start_mac.sh` / `start_windows.ps1` pairing. This costs
nothing in duplication: each wrapper is ~3 lines of actual code over a shared runner, and neither shell
contains a single line of allocation logic. The developer machine here is Windows/PowerShell; the `.sh`
files cover Git Bash, macOS and CI containers. Verified: the same `--seed 7` produces the identical book
from both shells.

Four PowerShell-specific notes, each of which was a real failure mode, and the last two were found
only by running the thing:

- **No `param()` block in the wrappers.** Every flag belongs to the Python tool; a `param` block makes
  PowerShell try to bind `--yes` itself and fail. `$args` passes through verbatim.
- **No `$ErrorActionPreference = "Stop"`**, unlike `start_windows.ps1`. Under `Stop`, PowerShell 5.1
  wraps a native command's stderr in a `NativeCommandError` blob, burying the one clear line the tool
  printed under a stack trace.
- **The Store `python.exe` stub.** Windows ships a fake `python.exe` that exits without running
  anything, so `Get-FinallyRunner` probes `sys.version_info` rather than trusting that the name resolves.
- **The comma is PowerShell's ARRAY operator.** `--tickers MU,AMD,SLV` arrives as a *nested array*, and
  binding it to a `[string[]]` parameter stringifies it space-joined — so the tool received one ticker
  named `"MU AMD SLV"` and reported it unpriced. `Invoke-PortfolioTool` takes `[object[]]` and re-joins
  any array element with commas, which reproduces exactly what was typed. Quoted (`"MU,AMD,SLV"`) and
  unquoted forms now behave identically; both are verified.

**Executable bit:** `core.fileMode` is `false` in this repo and the existing `start_mac.sh` is committed
`100644`, so the `.sh` files land non-executable too. On macOS/Linux run them as `bash scripts/…sh` or
`chmod +x` once — the same as every other script here. PowerShell does not use an exec bit.

---

## 2. Why the logic lives in Python, not in bash

The scripts must parse JSON, do floating-point division, and iterate. Bash does none of the three
without either `jq` (not guaranteed present) or `bc` (likewise), and the two scripts share ~90% of their
behaviour — writing that twice in `sh` and twice again in PowerShell means four copies of the same
arithmetic drifting apart.

So: one stdlib-only Python file (`urllib.request`, `json`, `random`, `argparse` — no new dependency, not
even `httpx`), and four wrappers that pass flags. The allocation functions come out pure and testable
with the pytest suite that already exists.

`scripts/lib_portfolio_tool.sh` resolves a runner in this order, and each wrapper sources it:

```bash
python3 / python  (>= 3.9)                         # preferred: no lockfile check per run
uv --directory backend run python scripts/portfolio_tool.py
docker exec -i finally python /app/scripts/portfolio_tool.py --base http://127.0.0.1:8000
```

The third works because the Dockerfile's `COPY backend/ ./` already carries `backend/scripts/` into the
image at `/app/scripts/`. No Dockerfile change is needed. The `--base` override is **prepended**, not
appended, so a user-supplied `--base` still wins — argparse takes the last occurrence.

### Three things running it actually caught

1. **A Windows console cannot print `≈` or `—`.** The default code page is cp1252 and
   `UnicodeEncodeError` killed the script *after* the trades had already gone through, leaving a
   half-built book and a traceback. Every string this tool prints is now ASCII. This is not a
   theoretical portability note — it happened on the first run.
2. **`sys.stdin.isatty()` is not a reliable "someone can answer" test under Git Bash.** With stdin
   redirected from `/dev/null` it still reported a terminal, so `input()` died on `EOFError` with a
   traceback instead of the one line telling the user to pass `--yes`. `confirm()` now catches both.
3. **`--dry-run` reset the portfolio.** The flag documents "print the plan, execute nothing", and it did
   print the plan — after `POST /api/portfolio/reset` had already emptied the account, and after any
   unwatched `--tickers` entry had been POSTed to the watchlist. The one flag a cautious user reaches
   for first was the most destructive way to try the script. Guarded now, with four tests in
   `test_portfolio_tool.py` that fail against the old code.

---

## 3. What each script produces, and why that shape

### `equal_weight_portfolio.sh` — equal **dollar** weight

Invests a fraction of cash equally across N tickers. `quantityᵢ = (cash · invest / n) / priceᵢ`.

This is the interesting control case, not a trivial one: an equal-weight portfolio is balanced by
*weight* and badly unbalanced by *risk*. In this universe ALAB carries σ=1.06 and SLV σ=0.75, and
LRCX/AMAT are 0.90-correlated near-duplicates — so 10% in each name puts wildly unequal amounts of
volatility on the book. The Risk button should show flat weight bars beside lopsided risk-share bars,
and `risk_parity` should have a large, visible correction to propose. That contrast is exactly what E2E
scenario 1 in `PORTFOLIO_ANALYTICS.md` §9 asserts.

### `start_random_portfolio.sh` — concentrated and lopsided

Picks a random subset and random weights from a Dirichlet-style draw, low concentration parameter by
default so the result is genuinely skewed (one or two names carrying most of the book), not
quasi-uniform. Reproducible via `--seed`.

This is the input that makes `min_variance` produce a large, obviously-correct correction — and the one
the E2E asserts `after.volatility ≤ before.volatility` on.

**Both scripts must be reproducible.** `--seed` defaults to `42`, not to system entropy. A test that
seeds a different portfolio each run cannot assert on anything but tautologies.

---

## 4. CLI

```
portfolio_tool.py {equal,random,save,load} [options]

shared:
  --base URL          default http://localhost:8000 (honours $FINALLY_PORT)
  --yes / -y          skip the confirmation prompt (required for CI / E2E)
  --json              machine-readable summary on stdout, for the E2E to read

equal / random:
  --tickers A,B,C     default: the current watchlist
  --count N           random mode only; default 6, sampled from the watchlist
  --invest F          fraction of cash to deploy; default 0.95
  --seed N            RNG seed; default 42
  --concentration A   random mode Dirichlet alpha; default 0.6 (low = lopsided)
  --no-reset          skip POST /api/portfolio/reset and build on top of what is there
  --dry-run           print the plan, execute nothing

save / load:
  --name NAME         sessions/NAME.json; default 'default'
  --file PATH         explicit path instead of --name
```

The shared flags live on a parent parser, not the top level, so they work **after** the subcommand
(`… equal --yes`) — which is the only ordering the shell wrappers can produce.

### Flow

1. Poll `GET /api/health` until `status == "ok"` (up to 60s). The health check already means *schema
   applied and the market task ticking* — so a price exists for every watchlist name before any trade
   is attempted.
2. Unless `--no-reset`: confirm, then `POST /api/portfolio/reset` → known $10,000 and the seed
   watchlist. **Prompt unless `--yes`** — reset destroys positions, trades, snapshots and chat history,
   and a script that silently wipes a demo account the moment it is run is a script nobody trusts twice.
3. `GET /api/watchlist` for tickers and live prices. Any `--tickers` not present are `POST`ed to the
   watchlist first; this also pulls them into the tracked set so they have a price to fill at.
4. Compute the allocation (pure function, see §5).
5. `POST /api/portfolio/trade` per ticker, **sequentially**, stopping on the first error with the
   backend's structured `detail` printed verbatim. Never parallel: each buy is validated against the
   cash the previous buys left, exactly as PLAN.md §9 requires of the LLM path.
6. `GET /api/portfolio`, print a table of ticker / quantity / price / value / weight, plus realised
   cash, and how far each weight landed from its target.

### Drift is expected — say so in the output

Prices tick every ~500ms and the trades are sequential, so realised weights land within roughly ±0.5%
of target rather than exactly on it. The script prints target and realised side by side; the E2E asserts
with a tolerance, not equality. Deploying 95% rather than 100% of cash by default exists for the same
reason: a 100% target makes the final buy race an upward tick and fail.

---

## 5. Pure functions (what the unit tests target)

```python
def equal_weights(tickers: list[str]) -> dict[str, float]
def random_weights(tickers: list[str], rng: random.Random, alpha: float) -> dict[str, float]
def to_orders(weights, prices, cash, invest) -> list[Order]   # Order(ticker, side, quantity)
```

Tests (`backend/tests/test_portfolio_tool.py`, no network — feed them dicts):

- weights sum to 1.0 in both modes
- `random_weights` with the same seed is identical across runs; different seeds differ
- low `alpha` yields a higher mean max-weight than high `alpha` — the concentration knob actually does
  something
- `to_orders` never exceeds `cash · invest`; quantities are **truncated** to 4dp, not rounded
- orders reconstruct the target weights to 1e-4
- an unpriced or zero-priced ticker is skipped with a warning rather than dividing by zero
- dust legs below `MIN_NOTIONAL` are dropped with a warning

**Truncation, not rounding.** Rounding up on every leg can push the batch past the budget, and it is
the *last* order that then fails — after the rest have already filled, leaving a half-built portfolio
and an error that points at the wrong ticker.

The HTTP layer stays a thin, untested shell around these — the E2E covers it end to end.

---

## 6. Portfolio sessions (save / load)

A session is the portfolio state as a JSON document: cash, positions **with their real average costs**,
and the watchlist. `save_session.sh` writes one; `load_session.sh` restores it.

```bash
./scripts/start_random_portfolio.sh --yes --seed 7
./scripts/save_session.sh --name lopsided          # -> sessions/lopsided.json
# …trade, experiment, rebalance, break things…
./scripts/load_session.sh --name lopsided --yes    # exactly back to the saved book
```

### Why this needed backend endpoints

A save is easy — `GET /api/portfolio` already has everything. A **load is not expressible through the
existing API**, and that is the whole reason `GET`/`POST /api/session` exist:

- there is no endpoint that sets cash to an arbitrary value; `reset` only sets $10,000
- there is no endpoint that sets an average cost

Replaying the positions as market buys would fill at *today's* price, so every cost basis and the cash
balance would come back wrong — and every unrealised P&L number in the "restored" account with them.
For a harness whose job is reproducibility, a lossy restore is worse than none.

### `GET /api/session`

Emits `{version, saved_at, cash_balance, positions[], watchlist[], meta}`. Cash, quantities and average
costs are **unrounded**: a save file exists to round-trip a state, and rounding cash to the cent turns
"restore" into "restore, minus a few cents that then compound through every later trade". `meta`
(mode, total value, `all_priced`) is informational and is ignored on import — prices move, so a
document loaded tomorrow cannot reproduce today's total value and should not pretend to.

### `POST /api/session`

Validates, then replaces cash, positions and the watchlist in **one transaction**, under the same
`trade_lock()` as `/portfolio/reset` and for the same reason (`Back_end_review.md` P1): an in-flight
trade already past its price read would otherwise commit on top of the just-restored tables. The
tracked-set read and `sync_tracked` are inside the lock too.

Decisions worth keeping:

| Question | Decision |
|---|---|
| Trades and P&L snapshots? | **Cleared.** They record how the account reached a state it is no longer in; a P&L chart splicing the old account's values onto the restored one is worse than an empty chart |
| Chat history? | **Kept.** This restores a portfolio, not the whole app. `reset` is the full wipe |
| Duplicate ticker in `positions`? | **400.** Merging two rows would invent an average cost the user never held |
| `quantity: 0`? | **422.** That is the phantom position a full sell deletes (`Review.md` B11); restoring one puts it straight back |
| Unknown `version`? | **400**, naming the version this build reads. Guessing at an unknown shape is how you silently corrupt an account |
| Snapshot after load? | **Yes**, immediately — the import just emptied the P&L table, and without it the chart stays blank until the 30s task next fires |
| Ticker case/whitespace? | Normalised through the same `normalize_ticker` every other route uses, so a hand-edited file with `" mu "` works |

Hand-editing a session file is a supported workflow — it is the fastest way to author a specific
portfolio for a test without trading into it.

### Two save formats, on purpose

| | `save_session` / `load_session` | `save_list` / `load_list` |
|---|---|---|
| Format | JSON, `sessions/NAME.json` | plain text, `suggested/NAME.txt` |
| Contents | cash, quantities **and average costs** | ticker + quantity only |
| Restores by | writing the rows directly (`POST /api/session`) | **resetting to $10,000 and buying at market** |
| Faithful? | exact, to the cent | quantities yes, cost basis and cash no |
| For | reproducing a book precisely — E2E fixtures, before/after comparisons | designing a book by hand |

The list format exists because the session JSON is faithful and horrible to edit: nested
objects, unrounded floats, an average cost per row. Nobody wants to hand-tune a column of
quantities in that. A list is what you actually want to type:

```
MU     4          # a big semiconductor bet
AMD:   6
SLV,   40
PLTR = 9
```

Space, comma, colon and equals all separate; `#` starts a comment; a row that cannot be read
is **reported and skipped**, never fatal — one fat-fingered line should not throw away the
other nine. A repeated ticker is skipped rather than summed, because two rows for one name
have no single correct reading.

`load_list` **buys at market** rather than writing quantities into the database, so the book
is built exactly the way a person would build it — through validation, the trade blotter and
the watchlist auto-add. That means it can run out of cash, so the cost is checked against the
balance *before the first order*: a rejection halfway through leaves a half-built portfolio,
which is precisely what a list is supposed to prevent. The error names the shortfall and the
percentage to scale by.

### Importing a real brokerage account WITH dates

`import_broker` below keeps only the proportions, because without dates the CEDEAR ratios are
unknown. `import_broker_with_dates` reads a **dated** transaction export instead, and that changes
the arithmetic entirely: the ratios become measurable (`us_close / cedear_price_usd`), the ARS/USD
rate falls out of the same-day bond conversion rows, and the real book can be rebuilt day by day.
`load_history` then restores it through `POST /api/session` with real cost bases.

Full design in `planning/PORTFOLIO_HISTORY.md`. The rest of this section covers the
proportions-only path, which is still the right tool when all you have is a holdings snapshot.

### Importing a real brokerage account

`import_broker` reads an Argentine broker's holdings export and writes a weights list.

**Weights, not share counts, and that is the whole design decision.** Those holdings are
CEDEARs — certificates over a *fraction* of a US share, at a ratio that differs per stock,
priced in pesos. 100 MU CEDEARs is not 100 MU shares, and a nine-figure peso book is not a
$10,000 one. The share counts in that file are meaningless here in every respect but one:
the proportions they represent. So the proportions are what carries over, and `load_list`
sizes them against whatever cash it has.

Which is why the list format grew a second row shape:

```
MU     4          # 4 shares
MU     10.08%     # 10.08% of the book
```

Mixing the two in one file raises rather than guesses — "4 shares of MU and 30% of AMD" has
no combined reading.

The export is an undocumented table flattened across five lines per holding, with the *next*
record's ticker riding on the end of the market-value line. Every row is checked against its
own arithmetic — `quantity × price` must equal the stated market value — and a row that fails
is reported and skipped. That check is free and it is the one that would catch a
decimal-separator mistake, which no looser check would: `305.650` parses perfectly well as
either 305,650 or 305.65, and getting it wrong misstates the holding by a factor of a
thousand. All 25 rows of the real file reconcile.

Rows without a `CEDEAR` prefix are locally-listed instruments with no US ticker (TGNO4, in
that file). They are dropped, named in the generated file's header, and the remaining weights
are renormalised so the book is not left under-invested by the dropped weight. `--keep-local`
overrides.

The same renormalisation applies at load time to anything FinAlly cannot price, for the same
reason: sizing against the original weights and dropping afterwards silently deploys less
cash than intended and still looks like it worked.

One wrinkle worth knowing: `save_list` immediately followed by `load_list` can fail. The
reset restores $10,000, and if the book has appreciated past that, rebuying the same
quantities costs more than the reset provides. That is the guard working, and the fix is to
scale by the percentage it prints.

---

## 7. Test loop these scripts enable

```bash
./scripts/equal_weight_portfolio.sh --yes
# → Risk & Return: weights flat, risk shares lopsided
# → Suggest Rebalance (risk_parity): risk shares converge to ~1/n

./scripts/start_random_portfolio.sh --yes --seed 7
# → Suggest Rebalance (min_variance): after.volatility ≤ before.volatility, always
# → Apply → re-open Risk → the realised numbers match `after` within drift tolerance
```

Round-trip check worth having: `equal_weight` seeding followed by a rebalance to the `equal_weight`
objective should propose **no trades at all** (every delta under the $10 dust threshold). If it proposes
trades, either the dust filter or the weight arithmetic is wrong. Cheap test, catches a whole class of
off-by-one.

---

## 8. E2E integration

`test/docker-compose.test.yml` runs the app plus Playwright with `LLM_MOCK=true`.

**Settled differently than this section first proposed.** The plan was to `docker compose exec` the tool
inside the app container so the E2E exercised the same code path a human runs. What shipped instead is a
`seedPortfolio()` helper in `test/tests/helpers.ts` that posts a document to **`POST /api/session`** —
because that endpoint did not exist when this section was written, and it is strictly better here:

- **Exact.** Trades fill at a price that ticks every 500ms, so a book built by trading is "40% MRVL ±
  drift" with whatever cost basis the tape printed. A session import writes quantities and average costs
  directly, so the spec asserts on the weights it actually asked for.
- **One call, no Python.** No dependency on an interpreter inside the Playwright container, and no
  reliance on `docker compose exec` being available to the test runner.
- **No duplicated logic.** The allocation maths the shell scripts use is still tested once, in
  `backend/tests/test_portfolio_tool.py`. The E2E needs a *portfolio*, not an allocator.

The seeder scripts remain the human-facing path and are unaffected.

---

## 9. Safety

- Every script is read-modify-write against a **simulated** account with fake money. The destructive
  acts are `POST /api/portfolio/reset` (seeding) and `POST /api/session` (load), both gated behind a
  prompt; `--yes` skips it and is required non-interactively, so nothing can silently wipe a demo
  account. Seeding can also skip the reset entirely with `--no-reset`.
- The seeders are idempotent in the useful sense: run twice with the same `--seed` and you land in the
  same place, because step 2 resets first.
- `save` is read-only: it writes a file and touches nothing in the app.
- No script touches Docker, the volume, or the database file directly. Everything goes through the
  public API, so the trade lock, validation, tracked-set sync and post-trade snapshots all apply exactly
  as they do for a human clicking Buy.
- `--dry-run` prints the full plan without sending a single write.

## 10. Docs updated when this landed

- `PLAN.md` §4 — the `scripts/` listing gains the five new files and `sessions/`
- `PLAN.md` §8 — the endpoint tables gain `GET`/`POST /api/session`
- `CLAUDE.md` — "Running it" gains the harness one-liners; test count 224 -> 248
