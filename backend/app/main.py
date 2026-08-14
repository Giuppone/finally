"""FastAPI application entrypoint.

Run with exactly ONE uvicorn worker (PLAN.md §3, §11): the price cache and the
market-data task live in process memory.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import db, routes
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


@app.get("/api/health")
async def health() -> dict:
    """Reports whether the schema is applied and the market task is alive, not just
    `{"status": "ok"}` — a compose `depends_on: service_healthy` gate reads this
    (Review.md D6)."""
    database = await db.run(db.stats)
    market = app.state.market.health()
    ok = database["ready"] and market["healthy"]
    return {"status": "ok" if ok else "degraded", "database": database, "market": market}


# NOTE: StaticFiles(html=True) mounts at "/" LAST, after every API router — mounting it
# earlier makes it swallow /api/* (Review.md C5).
