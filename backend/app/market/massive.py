"""Massive (formerly Polygon.io) integration. Endpoint shapes verified in MASSIVE_API.md.

Two facts dominate this module: Basic allows 5 requests/minute, and both snapshot
endpoints 403 on Basic. So the only viable free-tier source is the one endpoint that
returns many tickers per call — the grouped daily aggregate.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections import deque
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from typing import Any

from .models import Tick
from .seeds import SEED_PRICES
from .simulator import SimulatedSource
from .source import Entitlement, MarketDataSource

log = logging.getLogger(__name__)

FREE_TIER_RPM = 5
PAID_TIER_RPM = 300
GROUPED_LOOKBACK_DAYS = 7
SNAPSHOT_CHUNK = 250                              # v3 ticker.any_of hard limit


class RateLimiter:
    """Sliding window: at most `rate` acquisitions in any `per`-second window.

    A token bucket was the obvious choice and is wrong here. Starting full, it lets five
    requests through instantly and refills a sixth about twelve seconds later — six inside
    a rolling minute, against a limit advertised as five. Massive enforces a rolling
    window, so the startup path (one entitlement probe plus up to seven grouped-daily
    walkback attempts) would draw 429s exactly when the app has no prices yet.

    Still best-effort rather than a hard contract: an auto-paginating generator spends more
    than one HTTP request per acquire (see `call_list`).
    """

    def __init__(self, rate: int, per: float = 60.0) -> None:
        self._rate = max(1, int(rate))
        self._per = per
        self._issued: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:                     # holding across sleep serialises callers
            while True:
                now = time.monotonic()
                while self._issued and now - self._issued[0] >= self._per:
                    self._issued.popleft()
                if len(self._issued) < self._rate:
                    self._issued.append(now)
                    return
                await asyncio.sleep(self._per - (now - self._issued[0]) + 1e-3)


class MassiveGateway:
    """Owns the RESTClient, the rate limiter, and the to_thread boundary.

    The `massive` SDK is synchronous urllib3 with retries=3 and a 10s read timeout, so a
    single un-offloaded call can stall the event loop — and therefore every SSE connection
    in the process — for ~30 seconds (Review.md C1). Nothing else in the package may call
    the client directly.
    """

    def __init__(self, api_key: str, rpm: int = FREE_TIER_RPM) -> None:
        from massive import RESTClient          # imported here so the package is optional

        self._client = RESTClient(api_key=api_key)   # explicit beats the env-var default
        self._limiter = RateLimiter(rpm)

    @property
    def client(self) -> Any:
        return self._client

    def set_rpm(self, rpm: int) -> None:
        self._limiter = RateLimiter(rpm)

    async def call(self, fn: Callable[..., Any], /, **kwargs: Any) -> Any:
        await self._limiter.acquire()
        return await asyncio.to_thread(functools.partial(fn, **kwargs))

    async def call_list(self, fn: Callable[..., Iterable[Any]], /, **kwargs: Any) -> list[Any]:
        """For generator endpoints (`list_aggs`, `list_universal_snapshots`). The generator
        MUST be drained inside the thread — returning it to the loop would move the blocking
        HTTP calls back onto the event loop, one per page."""
        await self._limiter.acquire()
        return await asyncio.to_thread(lambda: list(fn(**kwargs)))


async def probe_entitlement(gateway: MassiveGateway) -> Entitlement:
    """One request decides the mode for the whole process. A 403 is permanent for the life
    of the key, so this runs once at startup and is never retried in a loop.

    Never raises — a bad market-data key must not crash startup. The asymmetry with
    OPENROUTER_API_KEY is deliberate: chat is unusable without its key, market data always
    has a working fallback.
    """
    try:
        await gateway.call(
            gateway.client.get_snapshot_all, market_type="stocks", tickers=["AAPL"]
        )
        return Entitlement.SNAPSHOTS
    except Exception as exc:                       # noqa: BLE001 — SDK raises broadly
        text = str(exc)
        if "NOT_AUTHORIZED" in text or "403" in text:
            return Entitlement.AGGREGATES
        if "401" in text or "UNAUTHORIZED" in text:
            return Entitlement.NONE
        log.warning("Massive entitlement probe inconclusive: %s", exc)
        return Entitlement.AGGREGATES              # aggregates is the safer assumption


class MassiveAnchorProvider:
    """Session anchors from the last completed trading session's close.

    Previous close — not the session open — because that is what every finance site and
    Massive's own change_percent use, and because day.open is 0 pre-market, which would
    divide by zero (Review.md B5 / D12).

    One grouped-daily call resolves every ticker at once and, because it returns all
    ~12,400 US symbols, doubles as the symbol-validation universe at zero extra cost.
    """

    def __init__(self, gateway: MassiveGateway) -> None:
        self._gw = gateway
        self._universe: dict[str, float] = {}      # ticker -> close, whole US market
        self._universe_session: str = ""
        self._lock = asyncio.Lock()

    # ---- AnchorProvider ---------------------------------------------
    async def anchors(self, tickers: list[str], session_date: str) -> dict[str, float]:
        universe = await self._ensure_universe(session_date)
        resolved: dict[str, float] = {}
        missing: list[str] = []
        for ticker in tickers:
            close = universe.get(ticker)
            if close:
                resolved[ticker] = close
            else:
                missing.append(ticker)

        for ticker in missing:                     # rare: IPO'd today, or a bad symbol
            close = await self._previous_close(ticker)
            if close:
                resolved[ticker] = close
            elif ticker in SEED_PRICES:
                log.warning("Massive has no bar for %s; using the seed table", ticker)
                resolved[ticker] = SEED_PRICES[ticker]
            else:
                log.warning("no anchor for %s; it will be skipped", ticker)
        return resolved

    async def is_known(self, ticker: str, session_date: str) -> bool:
        # Same session key as `anchors`, so a cold validate-then-add flow costs ONE grouped
        # request, not two. Keying off the cached label instead would load the universe
        # under "" and force a reload on the very next anchor lookup.
        universe = await self._ensure_universe(session_date)
        return ticker in universe

    async def refresh(self, session_date: str) -> None:
        async with self._lock:
            self._universe = {}
            self._universe_session = ""
        await self._ensure_universe(session_date)

    # ---- internals ---------------------------------------------------
    async def _ensure_universe(self, session_date: str) -> dict[str, float]:
        async with self._lock:
            if self._universe and self._universe_session == session_date:
                return self._universe
            self._universe = await self._load_universe()
            self._universe_session = session_date
            return self._universe

    async def _load_universe(self) -> dict[str, float]:
        """Walk back from YESTERDAY until a grouped call returns rows.

        Starting at yesterday rather than today is deliberate and matters on both tiers:
        on Basic today always returns empty (end-of-day data only), and on Starter+ today
        returns an IN-PROGRESS bar, which would make 'previous close' mean 'right now' and
        pin daily change at ~0%. The most recent completed session before today is the
        correct denominator during the session and after the close alike.
        """
        day = date.today() - timedelta(days=1)
        for _ in range(GROUPED_LOOKBACK_DAYS):
            try:
                bars = await self._gw.call(
                    self._gw.client.get_grouped_daily_aggs,
                    date=day.isoformat(),
                    adjusted=True,
                )
            except Exception as exc:               # noqa: BLE001
                log.warning("grouped daily call failed for %s: %s", day, exc)
                bars = None
            if bars:
                log.info("Massive grouped session %s: %d symbols", day, len(bars))
                return {b.ticker: b.close for b in bars if b.ticker and b.close}
            day -= timedelta(days=1)               # weekend / holiday
        log.error("no Massive session data in the last %d days", GROUPED_LOOKBACK_DAYS)
        return {}

    async def _previous_close(self, ticker: str) -> float | None:
        try:
            # NOTE: the SDK annotates this as a single object, but BaseClient maps the
            # deserializer over the results array — at runtime it is a LIST.
            # (MASSIVE_API.md §5.2.)
            aggs = await self._gw.call(
                self._gw.client.get_previous_close_agg, ticker=ticker, adjusted=True
            )
            for agg in aggs or []:
                if agg.close:
                    return float(agg.close)
        except Exception as exc:                   # noqa: BLE001
            log.warning("previous-close lookup failed for %s: %s", ticker, exc)
        return None


class MassiveLiveSource(MarketDataSource):
    """Polls real prices. Only reachable when the entitlement probe returns SNAPSHOTS."""

    def __init__(self, gateway: MassiveGateway, poll_interval: float = 15.0) -> None:
        self._gw = gateway
        self.poll_interval = poll_interval

    async def prime(self, tickers: list[str], anchors: dict[str, float]) -> None:
        return None                                # the API is authoritative

    async def poll(self, tickers: list[str]) -> list[Tick]:
        ticks: list[Tick] = []
        for i in range(0, len(tickers), SNAPSHOT_CHUNK):
            chunk = tickers[i:i + SNAPSHOT_CHUNK]
            snapshots = await self._gw.call_list(
                self._gw.client.list_universal_snapshots,
                type="stocks",
                ticker_any_of=chunk,
                limit=SNAPSHOT_CHUNK,              # default is 10 — silently truncates
            )
            for snap in snapshots:
                tick = self._to_tick(snap)
                if tick:
                    ticks.append(tick)
        return ticks

    async def release(self, ticker: str) -> None:
        return None                                # stateless

    @staticmethod
    def _to_tick(snap: Any) -> Tick | None:
        # A bad ticker fails INSIDE the results array, not as an HTTP error.
        if getattr(snap, "error", None):
            log.warning("snapshot error for %s: %s", snap.ticker, getattr(snap, "message", ""))
            return None
        session = getattr(snap, "session", None)
        price = getattr(session, "price", None) or getattr(session, "close", None)
        if not price:
            return None
        raw_ts = getattr(session, "last_updated", None)
        ts = raw_ts / 1e9 if raw_ts else time.time()      # v3 timestamps are NANOseconds
        return Tick(snap.ticker, float(price), ts)


class HybridLiveSource(MarketDataSource):
    """Massive while the market is open; GBM motion anchored to the last real print when
    it is closed. Optional — skip it and the app still works, it just looks frozen at
    night, which is when a UTC-3 developer demos it (Review.md C2)."""

    STATUS_TTL = 60.0

    def __init__(
        self,
        live: MassiveLiveSource,
        sim: SimulatedSource,
        gateway: MassiveGateway,
    ) -> None:
        self._live, self._sim, self._gw = live, sim, gateway
        self.poll_interval = live.poll_interval
        self._status = "unknown"
        self._checked = 0.0

    async def prime(self, tickers: list[str], anchors: dict[str, float]) -> None:
        await self._live.prime(tickers, anchors)
        await self._sim.prime(tickers, anchors)

    async def poll(self, tickers: list[str]) -> list[Tick]:
        if await self._market_open():
            ticks = await self._live.poll(tickers)
            for tick in ticks:                    # keep the engine at the real level
                await self._sim.rebase(tick.ticker, tick.price)
            return ticks
        return await self._sim.poll(tickers)

    async def release(self, ticker: str) -> None:
        await self._live.release(ticker)
        await self._sim.release(ticker)

    async def rebase(self, ticker: str, price: float) -> None:
        await self._sim.rebase(ticker, price)

    @property
    def market_status(self) -> str:
        return self._status

    async def _market_open(self) -> bool:
        now = time.monotonic()
        if now - self._checked > self.STATUS_TTL:
            try:                                   # free on every plan, real-time
                status = await self._gw.call(self._gw.client.get_market_status)
                self._status = status.market or "unknown"
            except Exception as exc:               # noqa: BLE001
                log.warning("market status check failed: %s", exc)
            self._checked = now
        return self._status == "open"
