"""Daily portfolio evolution and daily closes.

Sibling routes under `/api/history`, deliberately NOT extra parameters on the two endpoints
that already exist:

  * `/api/portfolio/history` reads `portfolio_snapshots` - the *simulated* $10,000 account, at
    30-second granularity, mutated by every trade, and denominated against `STARTING_CASH`.
    This serves the *real* brokerage account, daily, immutable, six figures. One path returning
    both would make `total_value` mean different things depending on a query parameter, and the
    frontend would have to branch on its own input anyway.
  * `/api/prices/{ticker}/history` is `Depends(get_service)` and 404s when the ticker is not
    *tracked*. Daily closes exist for anything in the bars cache, tracked or not, so an
    `interval=1d` switch would force either loosening that 404 - a behaviour change to the
    route the main chart depends on - or refusing daily data for untracked names.

Timestamps: ISO-8601 **dates**, plus an epoch-ms `ts` alongside. A daily close has no time of
day, and stamping one `T00:00:00Z` shifts it a day for any viewer west of UTC; but the market
data convention in this project is epoch-ms, and carrying both lets one chart component read
this and the live ring buffer through the same accessor.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from . import bars as bars_module
from . import ledger as ledger_module
from . import session as session_module
from .reconstruct import Reconstruction, build

router = APIRouter(prefix="/api/history", tags=["history"])

# No "1y". The bars cache starts 2025-12-07, so a year range would silently return eight
# months and label it a year.
RANGES: dict[str, int | None] = {
    "1m": 31, "3m": 92, "6m": 183, "ytd": None, "max": None,
}
DEFAULT_RANGE = "max"

_lock = asyncio.Lock()


@dataclass(frozen=True)
class _Cached:
    stamp: tuple
    value: Reconstruction | None


def _stamp() -> tuple:
    """File identity of both inputs.

    In the image these are baked in and never change, so the reconstruction runs once per
    process. In a checkout, regenerating `ledger.json` or refreshing `bars.json` is picked up
    on the next request with no restart.
    """
    def one(path: Path) -> tuple:
        try:
            stat = path.stat()
        except OSError:
            return (str(path), 0, 0)
        return (str(path), stat.st_mtime_ns, stat.st_size)

    return (one(ledger_module.default_path()), one(bars_module.bars_path()))


def _compute() -> Reconstruction | None:
    document = ledger_module.load_document()
    if document is None:
        return None
    return build(document, bars_module.load())


async def reconstruction(request: Request) -> Reconstruction | None:
    """The memoised curve, or None when no ledger has been generated.

    Computed lazily rather than in `lifespan`: the current startup is deliberate about what may
    abort a boot (`chat.verify_config()` is the only fail-fast, and it is documented as such).
    A missing or malformed ledger must degrade this one panel, not the whole app.

    `to_thread` because a file read plus a few thousand float operations on the event loop is
    the same class of stall `db.run` exists to avoid - every SSE connection shares that loop.
    """
    stamp = _stamp()
    cached: _Cached | None = getattr(request.app.state, "history", None)
    if cached is not None and cached.stamp == stamp:
        return cached.value

    async with _lock:
        # Re-check: another request may have computed it while this one waited.
        cached = getattr(request.app.state, "history", None)
        if cached is not None and cached.stamp == stamp:
            return cached.value
        value = await asyncio.to_thread(_compute)
        request.app.state.history = _Cached(stamp=stamp, value=value)
        return value


def _epoch_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)


def _window(days: list[date], span: str) -> list[date]:
    if not days or span == "max":
        return days
    last = days[-1]
    if span == "ytd":
        floor = date(last.year, 1, 1)
    else:
        floor = last - timedelta(days=RANGES[span] or 0)
    return [day for day in days if day >= floor]


def _meta(result: Reconstruction | None) -> dict:
    cache = bars_module.load()
    meta = {
        "bars_through": cache.as_of.isoformat() if cache.as_of else None,
        "bars_from": cache.start.isoformat() if cache.start else None,
        "bars_fetched_at": cache.fetched_at or None,
        "ranges": list(RANGES),
    }
    if result is None:
        return meta
    first, last = (result.fx_points[0][1], result.fx_points[-1][1]) if result.fx_points else (0.0, 0.0)
    meta.update({
        "tickers_priced": len(result.priced),
        "tickers_carried": len(result.carried),
        "carried": result.carried,
        "fx_observations": len(result.fx_points),
        "fx_start": round(first, 2),
        "fx_end": round(last, 2),
        "opening_cash": round(result.opening_cash, 2),
        "opening_carry": round(result.opening_carry, 2),
    })
    return meta


_UNAVAILABLE = (
    "no reconstructed ledger on this build - run scripts/import_broker_with_dates to "
    "generate backend/calibration/ledger.json"
)


@router.get("/portfolio")
async def portfolio_curve(
    request: Request,
    range: str = Query(DEFAULT_RANGE, description="1m | 3m | 6m | ytd | max"),
) -> dict:
    """The daily USD value of the reconstructed book.

    Returns **200 with `available: false`** rather than 404 when no ledger has been generated.
    The frontend fetches this on every page load, so a stock deployment would otherwise log a
    404 on each one; the bulk price route sets the same precedent ("one stale symbol must not
    fail the batch").

    Both `total_value` and `return_pct` ship on every point. `return_pct` is rebased to the
    first point of the *filtered* window, which the client cannot compute correctly without
    refetching - so the $/% toggle is a field swap rather than a round trip.
    """
    span = range if range in RANGES else DEFAULT_RANGE
    result = await reconstruction(request)
    if result is None or not result.available:
        return {
            "available": False, "currency": "USD", "range": span, "points": [],
            "warnings": [_UNAVAILABLE] if result is None else result.warnings,
            "meta": _meta(result),
        }

    days = _window([point.day for point in result.points], span)
    selected = [point for point in result.points if point.day in set(days)]
    base = selected[0].total_value if selected else 0.0

    return {
        "available": True,
        "currency": "USD",
        "range": span,
        "start_date": selected[0].day.isoformat() if selected else None,
        "end_date": selected[-1].day.isoformat() if selected else None,
        "as_of": result.as_of.isoformat() if result.as_of else None,
        "base_value": round(base, 2),
        "points": [
            {
                "date": point.day.isoformat(),
                "ts": _epoch_ms(point.day),
                "total_value": round(point.total_value, 2),
                "return_pct": round((point.total_value / base - 1) * 100, 4) if base else 0.0,
                "positions_value": round(point.positions_value, 2),
                "carry_value": round(point.carry_value, 2),
                "cash_balance": round(point.cash_balance, 2),
            }
            for point in selected
        ],
        "warnings": result.warnings,
        "meta": _meta(result),
    }


@router.get("/prices")
async def bulk_daily(
    tickers: str = Query(..., description="Comma-separated symbols"),
    range: str = Query(DEFAULT_RANGE, description="1m | 3m | 6m | ytd | max"),
) -> dict:
    """Daily closes for several tickers at once.

    Mirrors `/api/prices/history`: a symbol with no bars yields an empty list rather than
    failing the batch, and is named in `unknown` so the caller can say so.
    """
    span = range if range in RANGES else DEFAULT_RANGE
    cache = bars_module.load()
    wanted = [entry.strip().upper() for entry in tickers.split(",") if entry.strip()]
    if not wanted:
        raise HTTPException(status_code=400, detail={"reason": "no tickers requested"})

    series: dict[str, list[dict]] = {}
    unknown: list[str] = []
    for ticker in wanted:
        points = cache.series(ticker)
        if not points:
            series[ticker] = []
            unknown.append(ticker)
            continue
        days = _window(sorted(points), span)
        series[ticker] = [
            {"date": day.isoformat(), "ts": _epoch_ms(day), "close": round(points[day], 4)}
            for day in days
        ]

    return {
        "series": series, "range": span, "unknown": unknown,
        "as_of": cache.as_of.isoformat() if cache.as_of else None,
    }


@router.get("/prices/{ticker}")
async def ticker_daily(
    ticker: str,
    request: Request,
    range: str = Query(DEFAULT_RANGE, description="1m | 3m | 6m | ytd | max"),
) -> dict:
    """Daily closes for one ticker - the main chart's non-LIVE ranges.

    404s on an unknown symbol, matching `/api/prices/{ticker}/history`: a single-resource route
    that returns an empty body for a name that does not exist hides a typo.

    When a reconstructed ledger exists, the response also carries the user's own trades in the
    window and the date they first held the name. Without those, the chart draws the ticker's
    market price back to the start of the bars cache, and a recently-opened position reads
    as one held all along - which is exactly the misreading it caused.
    """
    span = range if range in RANGES else DEFAULT_RANGE
    symbol = ticker.strip().upper()
    points = bars_module.load().series(symbol)
    if not points:
        raise HTTPException(
            status_code=404,
            detail={"reason": f"{symbol} has no daily history", "code": "no_daily_history"},
        )

    days = _window(sorted(points), span)

    trades: list[dict] = []
    held_since: str | None = None
    result = await reconstruction(request)
    if result is not None and days:
        lo, hi = days[0].isoformat(), days[-1].isoformat()
        trades = [
            {**event, "ts": _epoch_ms(date.fromisoformat(event["date"]))}
            for event in result.events.get(symbol, [])
            if lo <= event["date"] <= hi
        ]
        held_since = result.held_since.get(symbol)

    return {
        "ticker": symbol,
        "range": span,
        "start_date": days[0].isoformat() if days else None,
        "end_date": days[-1].isoformat() if days else None,
        "points": [
            {"date": day.isoformat(), "ts": _epoch_ms(day), "close": round(points[day], 4)}
            for day in days
        ],
        "trades": trades,
        "held_since": held_since,
    }


@router.get("/session")
async def history_session(request: Request) -> dict:
    """The reconstructed book as a document `POST /api/session` will accept.

    404 here rather than `available: false`, unlike `/portfolio`: this caller asked for one
    specific thing to load, and there is nothing to give it.

    Nested under `session` so the CLI can POST `body["session"]` verbatim, with no
    field-stripping guesswork about which top-level keys are part of the document.
    """
    result = await reconstruction(request)
    if result is None or not result.available:
        raise HTTPException(
            status_code=404, detail={"reason": _UNAVAILABLE, "code": "no_ledger"})

    document, dropped = session_module.to_session(result)
    return {"session": document, "dropped": dropped, "warnings": result.warnings}
