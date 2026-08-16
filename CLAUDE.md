# FinAlly Project - the Finance Ally

All project documentation is in the `planning` directory.

The key document is PLAN.md included in full below. Section 13 is a decision record resolving open questions from doc reviews — those decisions are already reflected in sections 1–12.

## Current state (verified 2026-08-15)

`backend/` was deliberately emptied once and **has since been rebuilt**. It is no longer empty:

- **Built and tested**: database + schema, the market-data package (three modes — see PLAN.md §13 item 9), SSE streaming, price cache with ring buffer, trading/portfolio/watchlist endpoints, 30s P&L snapshots. `cd backend && uv run pytest -q` → **191 passed**.
- **Not started**: chat/LLM (§9), the entire frontend (§10 — `frontend/` does not exist), and the Docker/scripts/E2E packaging (§11, §12). `main.py` carries a comment marking where the `StaticFiles` mount goes once a frontend exists.

Design notes from the deleted first pass are in `planning/archive/`; consult them only when required. The current design docs are `MARKET_DATA_DESIGN.md`, `MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md` and `MASSIVE_API.md`. Code reviews live in `Back_end_review.md` and `Market_data_review.md` — the three findings open at the time of those reviews (reset not serialised, duplicate snapshot rows, `add_ticker` perturbing other GBM paths) were **fixed on 2026-08-15**, each with a regression test.

## Running it

```bash
cd backend
uv run uvicorn app.main:app --port 8000      # needs env vars; see PLAN.md §5
uv run market_data_demo.py                   # live terminal dashboard, simulator
uv run market_data_demo.py --live            # same, against the real Massive key
```

The backend reads `os.environ` only and never parses `.env` (§5), so load the file into the environment before launching or it starts in `SIMULATED`.

@planning/PLAN.md