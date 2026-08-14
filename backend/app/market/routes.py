"""HTTP surface: the SSE price stream and the history endpoints charts seed from."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .deps import get_service
from .service import MarketDataService
from .symbols import InvalidTicker, normalize_ticker

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["market"])

TICK_INTERVAL = 0.5          # how often the generator CHECKS the cache
KEEPALIVE_S = 15.0           # comment frame to hold the connection through proxies
MAX_HISTORY = 1_000
DEFAULT_SPARK_POINTS = 60


def _frame(payload: dict) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


@router.get("/stream/prices")
async def stream_prices(
    request: Request,
    service: MarketDataService = Depends(get_service),
) -> StreamingResponse:
    return StreamingResponse(
        _price_events(request, service),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",     # stop nginx buffering the stream if proxied
        },
    )


async def _price_events(
    request: Request, service: MarketDataService
) -> AsyncIterator[str]:
    cache = service.cache
    client = request.client.host if request.client else "unknown"
    log.info("SSE connect: %s", client)

    yield "retry: 1000\n\n"
    yield _frame({
        "type": "hello",
        "mode": str(service.mode),
        "tick_ms": int(TICK_INTERVAL * 1000),
        "poll_interval_s": service.poll_interval,
        "session_date": service.session_date,
        "healthy": service.healthy,
        "quotes": [q.to_wire() for q in cache.snapshot().values()],
    })

    last_version = cache.version
    last_emit = asyncio.get_running_loop().time()
    try:
        while True:
            if await request.is_disconnected():
                break
            now = asyncio.get_running_loop().time()
            version = cache.version
            if version != last_version:
                last_version = version
                last_emit = now
                yield _frame({
                    "type": "prices",
                    "seq": version,
                    "healthy": service.healthy,
                    "quotes": [q.to_wire() for q in cache.snapshot().values()],
                })
            elif now - last_emit >= KEEPALIVE_S:
                last_emit = now
                yield ": ping\n\n"
            await asyncio.sleep(TICK_INTERVAL)
    except asyncio.CancelledError:
        raise
    finally:
        log.info("SSE disconnect: %s", client)


@router.get("/prices/history")
async def bulk_history(
    tickers: str = Query(..., description="Comma-separated symbols"),
    limit: int = Query(DEFAULT_SPARK_POINTS, ge=1, le=MAX_HISTORY),
    service: MarketDataService = Depends(get_service),
) -> dict:
    """Sparkline seeding in ONE call (D6). Ten separate per-ticker requests on mount is
    the alternative, and sparklines need ~60 points, not 1,000."""
    try:
        wanted = [normalize_ticker(t) for t in tickers.split(",") if t.strip()]
    except InvalidTicker as exc:
        raise HTTPException(400, str(exc)) from exc
    # An untracked ticker yields an empty list rather than a 404 — the frontend asks for a
    # whole watchlist at once and one stale symbol must not fail the batch.
    return {
        "series": {
            t: [{"ts": round(ts * 1000), "price": round(p, 4)}
                for ts, p in service.cache.history(t, limit)]
            for t in wanted
        }
    }


@router.get("/prices/{ticker}/history")
async def ticker_history(
    ticker: str,
    limit: int = Query(MAX_HISTORY, ge=1, le=MAX_HISTORY),
    service: MarketDataService = Depends(get_service),
) -> dict:
    """Main-chart seeding: the chart renders populated on first paint instead of filling
    in over minutes (PLAN.md §13 item 2)."""
    try:
        symbol = normalize_ticker(ticker)
    except InvalidTicker as exc:
        raise HTTPException(400, str(exc)) from exc
    if service.quote(symbol) is None:
        raise HTTPException(status_code=404, detail=f"{symbol} is not tracked")
    return {
        "ticker": symbol,
        "points": [{"ts": round(ts * 1000), "price": round(p, 4)}
                   for ts, p in service.cache.history(symbol, limit)],
    }
