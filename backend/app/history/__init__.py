"""Portfolio evolution: reconstructing a real, dated brokerage ledger as a daily USD curve.

Design and the reasoning behind the CEDEAR math: planning/PORTFOLIO_HISTORY.md.

`routes` is deliberately NOT re-exported here, unlike `app/analytics/__init__.py` which does
export its router. `backend/scripts/portfolio_tool.py` imports `app.history` under whatever
bare `python3` the host happens to have - `lib_portfolio_tool.sh` picks a plain interpreter
long before it considers `uv run` - and every module below is stdlib-only so that works.
Re-exporting the router would drag FastAPI into that import and fail with
`ModuleNotFoundError: fastapi` on a machine that has never installed the backend.

`main.py` therefore reaches for the router explicitly:

    from .history.routes import router as history_router

Please do not "tidy" this into the analytics shape.
"""

from __future__ import annotations

from . import bars, fx, ledger, reconstruct, session

__all__ = ["bars", "fx", "ledger", "reconstruct", "session"]
