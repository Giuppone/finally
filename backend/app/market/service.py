"""MarketDataService — owns the loop, the tracked set, the cache, the mode, the session."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from .cache import PriceCache
from .models import Quote
from .source import AnchorProvider, MarketDataSource, Mode

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")       # requires `tzdata` on the dependency list
SESSION_OPEN = dtime(9, 30)
MAX_BACKOFF = 60.0
UNHEALTHY_AFTER = 3


def current_session_date(now: datetime | None = None) -> str:
    """FinAlly's session label: the ET trading date, rolling at 09:30 ET.

    At 02:00 ET Tuesday this returns Monday — still inside Monday's session window as far
    as 'daily change' is concerned.

    Weekends roll back to Friday rather than producing Saturday and Sunday labels. A
    calendar label would make `_maybe_roll_session` fire three times over a weekend — Sat,
    Sun, and again before Monday's open — each time spending a grouped-daily walkback on a
    5-req/min key and, in ANCHORED mode, visibly rebasing the simulated path to the same
    Friday close it already had.

    US market holidays still produce one spurious roll; `_maybe_roll_session` absorbs it by
    skipping the rebase when the anchor has not actually moved. A real trading calendar
    would close that gap and is not worth the dependency here.
    """
    now_et = (now or datetime.now(tz=timezone.utc)).astimezone(ET)
    if now_et.time() < SESSION_OPEN:
        now_et -= timedelta(days=1)
    while now_et.weekday() >= 5:            # 5 = Saturday, 6 = Sunday
        now_et -= timedelta(days=1)
    return now_et.date().isoformat()


class MarketDataService:
    def __init__(
        self,
        source: MarketDataSource,
        anchors: AnchorProvider,
        cache: PriceCache,
        mode: Mode,
    ) -> None:
        self._source = source
        self._anchors = anchors
        self._cache = cache
        self.mode = mode

        self._tracked: set[str] = set()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()          # guards the tracked set, not the cache (§5.2)
        self._session_date = current_session_date()
        self._last_ok = 0.0
        self._failures = 0

    # ---- lifecycle ----------------------------------------------------
    async def start(self, tickers: set[str]) -> None:
        async with self._lock:
            await self._track_locked(set(tickers))
        self._task = asyncio.create_task(self._run(), name="market-data")
        log.info(
            "market data: mode=%s source=%s tracking=%d session=%s",
            self.mode, type(self._source).__name__, len(self._tracked), self._session_date,
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._source.aclose()

    # ---- reads (the API other modules use) ----------------------------
    def quote(self, ticker: str) -> Quote | None:
        return self._cache.get(ticker)

    def price(self, ticker: str) -> float | None:
        """None means 'no price yet' — callers fall back to avg_cost (D15)."""
        return self._cache.price(ticker)

    @property
    def cache(self) -> PriceCache:
        return self._cache

    @property
    def tracked(self) -> set[str]:
        return set(self._tracked)

    @property
    def session_date(self) -> str:
        return self._session_date

    @property
    def poll_interval(self) -> float:
        """Surfaced in the SSE hello frame so the client knows whether to expect a smooth
        tape (0.5s) or a stepped one (15s under LIVE)."""
        return self._source.poll_interval

    @property
    def healthy(self) -> bool:
        return self._failures < UNHEALTHY_AFTER

    async def validate(self, ticker: str) -> bool:
        # The session date goes with it: without it the provider caches its symbol universe
        # under an empty label and reloads on the next anchor call (see MassiveAnchorProvider).
        return await self._anchors.is_known(ticker, self._session_date)

    # ---- membership ---------------------------------------------------
    async def add_ticker(self, ticker: str) -> Quote | None:
        """Track one ticker and return it PRICED. Used by watchlist-add and by the LLM's
        auto-add path (PLAN.md §9). Resolves the anchor, seeds the cache, primes the
        source, and — in LIVE mode — polls that single ticker, all before returning."""
        async with self._lock:
            if ticker in self._tracked:
                return self._cache.get(ticker)

            resolved = await self._anchors.anchors([ticker], self._session_date)
            anchor = resolved.get(ticker)
            if anchor is None:
                log.warning("cannot anchor %s; not tracking it", ticker)
                return None

            quote = self._cache.seed(ticker, anchor, session_date=self._session_date)
            self._tracked.add(ticker)
            await self._source.prime([ticker], resolved)

        # One immediate poll so LIVE mode does not hand back a stale anchor as the fill
        # price. In SIMULATED/ANCHORED the anchor IS the price, so this costs one GBM step.
        with contextlib.suppress(Exception):
            for tick in await self._source.poll([ticker]):
                if tick.ticker == ticker:
                    quote = self._cache.apply(tick)
        return quote

    async def sync_tracked(self, tickers: set[str]) -> None:
        """Recompute the tracked set = watchlist ∪ open positions.
        Call after ANY watchlist change and after EVERY trade.

        Removals AND additions happen under one lock hold, so the whole reconciliation is
        atomic with respect to the desired set. Releasing between the two halves (so the
        anchor fetch runs unlocked) looks harmless but is not: starting from {A},
        concurrent reconciliations to {B} and {C} each compute their own addition against
        the same emptied set, both apply, and the service ends up tracking {B, C} — the
        union of two requests, not the last one.
        """
        async with self._lock:
            removed = self._tracked - tickers
            for ticker in removed:
                self._tracked.discard(ticker)
                await self._source.release(ticker)
                self._cache.evict(ticker)          # D16: buffer and anchor go too
            await self._track_locked(tickers)

    async def _track_locked(self, tickers: set[str]) -> None:
        """Resolve anchors for new tickers and prime the source. **Caller holds `_lock`.**"""
        new = sorted(tickers - self._tracked)
        if not new:
            return
        resolved = await self._anchors.anchors(new, self._session_date)
        primed: list[str] = []
        for ticker in new:
            anchor = resolved.get(ticker)
            if anchor is None:
                log.warning("no anchor for %s; skipping", ticker)
                continue
            self._cache.seed(ticker, anchor, session_date=self._session_date)
            self._tracked.add(ticker)
            primed.append(ticker)
        if primed:
            await self._source.prime(primed, resolved)

    # ---- session rollover ---------------------------------------------
    async def _maybe_roll_session(self) -> None:
        today = current_session_date()
        if today == self._session_date:
            return
        log.info("session roll: %s -> %s (mode=%s)", self._session_date, today, self.mode)
        async with self._lock:
            self._session_date = today
            await self._anchors.refresh(today)

            if self.mode is Mode.SIMULATED:
                # No real data to re-anchor against: pin the anchor to where the path is
                # now, so daily change restarts at 0.00% with no price discontinuity.
                for ticker in sorted(self._tracked):
                    quote = self._cache.get(ticker)
                    if quote:
                        self._cache.reanchor(ticker, quote.price, today)
                return

            fresh = await self._anchors.anchors(sorted(self._tracked), today)
            for ticker, anchor in fresh.items():
                # ANCHORED re-bases the simulated path onto the new real close; the
                # resulting discontinuity IS an overnight gap, which is realistic.
                # LIVE only moves the anchor — the API keeps supplying the price.
                #
                # An unchanged anchor means no new session actually printed (a market
                # holiday reached the weekday guard in current_session_date). Rebasing
                # then would yank the path back to a close it already gapped from, which
                # reads as a fake gap on a day the market never opened.
                quote = self._cache.get(ticker)
                unchanged = quote is not None and quote.open_price == anchor
                rebase = self.mode is Mode.ANCHORED and not unchanged
                self._cache.reanchor(ticker, anchor, today, rebase=rebase)
                if rebase:
                    await self._source.rebase(ticker, anchor)

    # ---- the one polling loop -------------------------------------------
    async def _run(self) -> None:
        while True:
            try:
                await self._maybe_roll_session()
                if self._tracked:
                    ticks = await self._source.poll(sorted(self._tracked))
                    for tick in ticks:
                        if tick.ticker in self._tracked:   # guard against a concurrent evict
                            self._cache.apply(tick)
                    self._last_ok = time.time()
                    self._failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                self._failures += 1
                log.exception("market poll failed (%d consecutive)", self._failures)
            await asyncio.sleep(self._backoff())

    def _backoff(self) -> float:
        base = self._source.poll_interval
        if not self._failures:
            return base
        return min(base * 2 ** min(self._failures, 5), MAX_BACKOFF)

    def health(self) -> dict[str, object]:
        """Fragment merged into GET /api/health."""
        return {
            "mode": str(self.mode),
            "source": type(self._source).__name__,
            "healthy": self.healthy,
            "tracked": len(self._tracked),
            "session_date": self._session_date,
            "last_tick_age_s": round(time.time() - self._last_ok, 2) if self._last_ok else None,
            "market_status": getattr(self._source, "market_status", None),
        }
