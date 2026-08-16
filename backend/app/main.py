"""FastAPI application entrypoint.

Run with exactly ONE uvicorn worker (PLAN.md §3, §11): the price cache and the
market-data task live in process memory.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import chat, db, routes
from .market import PriceCache, build_market_service
from .market import router as market_router
from .portfolio import SnapshotTask

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-5s %(name)-24s %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Before anything expensive: chat is a core feature, so a missing OPENROUTER_API_KEY
    # aborts the boot with a message naming the variable rather than yielding an app whose
    # chat panel 500s on first use (PLAN.md §5, §13 item 7).
    chat.verify_config()

    # Ordering is forced by a dependency PLAN.md leaves implicit: the market service needs
    # the watchlist to know what to track, so the schema must exist before the market task
    # starts. That is why init runs here and not on first request (Review.md D3).
    await db.initialize()

    cache = PriceCache()
    service = await build_market_service(cache)      # probes Massive, picks the mode
    app.state.market = service

    await service.start(await db.run(db.tracked_tickers))   # watchlist ∪ open positions

    snapshots = SnapshotTask(service)
    snapshots.start()
    app.state.snapshots = snapshots
    try:
        yield
    finally:
        await snapshots.stop()
        await service.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)
app.include_router(market_router)
app.include_router(routes.router)
app.include_router(chat.router)


@app.get("/api/health")
async def health() -> dict:
    """Reports whether the schema is applied and the market task is alive, not just
    `{"status": "ok"}` — a compose `depends_on: service_healthy` gate reads this
    (Review.md D6)."""
    database = await db.run(db.stats)
    market = app.state.market.health()
    ok = database["ready"] and market["healthy"]
    return {"status": "ok" if ok else "degraded", "database": database, "market": market}


# The frontend export mounts at "/" LAST, after every API router — mounting it earlier makes
# it swallow /api/* (Review.md C5). `html=True` serves index.html for "/" and falls back to
# it for unknown paths, which is what a single-page export needs.
#
# Guarded on the directory existing so a backend-only run (`uv run uvicorn app.main:app`,
# every pytest module, the market demo) still starts when nothing has been built yet. The
# image sets FINALLY_STATIC_DIR=/app/static, where the Node stage's output is copied.
def _mount_frontend() -> None:
    configured = os.environ.get("FINALLY_STATIC_DIR", "").strip()
    static_dir = Path(configured) if configured else Path(__file__).resolve().parents[2] / "frontend" / "out"
    if not static_dir.is_dir():
        log.info("no frontend build at %s — serving the API only", static_dir)
        return
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")
    log.info("serving frontend from %s", static_dir)


_mount_frontend()
