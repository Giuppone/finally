"""In-memory price cache — the single source of truth for "what is X worth right now"."""

from __future__ import annotations

import time
from collections import deque

from .models import Quote, Tick

HISTORY_MAXLEN = 1_000        # PLAN.md §6: ~8 min at 500ms, ~4h under a 15s Massive poll


class PriceCache:
    """Quote store. Mutated only on the event-loop thread — no lock (design §5.2)."""

    def __init__(self, history_maxlen: int = HISTORY_MAXLEN) -> None:
        self._quotes: dict[str, Quote] = {}
        self._maxlen = history_maxlen
        self._version = 0

    # ---- change detection for SSE (D4) -------------------------------
    @property
    def version(self) -> int:
        """Monotonic counter, bumped by every mutation. The SSE generator gates on it."""
        return self._version

    # ---- writes ------------------------------------------------------
    def seed(
        self,
        ticker: str,
        anchor: float,
        *,
        ts: float | None = None,
        session_date: str = "",
    ) -> Quote:
        """Create the entry and fix its anchor. Idempotent — never re-anchors."""
        existing = self._quotes.get(ticker)
        if existing is not None:
            return existing
        ts = time.time() if ts is None else ts
        quote = Quote(
            ticker=ticker,
            price=anchor,
            prev_price=anchor,
            open_price=anchor,
            ts=ts,
            session_date=session_date,
            history=deque([(ts, anchor)], maxlen=self._maxlen),
        )
        self._quotes[ticker] = quote
        self._version += 1
        return quote

    def apply(self, tick: Tick) -> Quote:
        """Record a new price. Never touches open_price or session_date."""
        quote = self._quotes.get(tick.ticker)
        if quote is None:
            # Self-seeding is deliberate: a source producing a ticker the service has not
            # seeded is a bug, but dropping the price would be a worse one.
            return self.seed(tick.ticker, tick.price, ts=tick.ts)
        quote.prev_price = quote.price
        quote.price = tick.price
        quote.ts = tick.ts
        quote.history.append((tick.ts, tick.price))
        self._version += 1
        return quote

    def reanchor(
        self,
        ticker: str,
        anchor: float,
        session_date: str,
        *,
        rebase: bool = False,
    ) -> Quote | None:
        """Roll the session anchor (D11). `rebase` also moves the live price to `anchor`,
        which reads as an overnight gap — used in ANCHORED mode only."""
        quote = self._quotes.get(ticker)
        if quote is None:
            return None
        quote.open_price = anchor
        quote.session_date = session_date
        if rebase:
            quote.prev_price = quote.price
            quote.price = anchor
            quote.ts = time.time()
            quote.history.append((quote.ts, anchor))
        self._version += 1
        return quote

    def evict(self, ticker: str) -> None:
        """Drop a ticker leaving the tracked set — quote, buffer and anchor (D16)."""
        if self._quotes.pop(ticker, None) is not None:
            self._version += 1

    # ---- reads -------------------------------------------------------
    def get(self, ticker: str) -> Quote | None:
        """None means 'no price yet'. Never fabricate one — see D15."""
        return self._quotes.get(ticker)

    def price(self, ticker: str) -> float | None:
        quote = self._quotes.get(ticker)
        return quote.price if quote else None

    def snapshot(self) -> dict[str, Quote]:
        return dict(self._quotes)

    def tickers(self) -> set[str]:
        return set(self._quotes)

    def history(self, ticker: str, limit: int | None = None) -> list[tuple[float, float]]:
        """Recent (ts, price) points, evenly subsampled when `limit` is smaller than the
        buffer. Subsampling — not truncation — is what makes a 60-point sparkline span the
        whole window instead of the last 30 seconds (D6)."""
        quote = self._quotes.get(ticker)
        if quote is None:
            return []
        points = list(quote.history)
        if limit is None or limit <= 0 or len(points) <= limit:
            return points
        stride = len(points) / limit
        sampled = [points[int(i * stride)] for i in range(limit)]
        sampled[-1] = points[-1]           # always end on the live price
        return sampled
