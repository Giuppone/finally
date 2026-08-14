"""Types that cross the market-data boundary. No provider SDK object escapes an adapter."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal

Direction = Literal["up", "down", "flat"]


@dataclass(frozen=True, slots=True)
class Tick:
    """One price observation. What a source produces; the cache consumes it."""

    ticker: str
    price: float
    ts: float          # float Unix seconds — normalised at the adapter (D14)


@dataclass(slots=True)
class Quote:
    """Cached state for one ticker. Mirrors the PLAN.md §6 cache entry."""

    ticker: str
    price: float
    prev_price: float
    open_price: float                      # session anchor; written once per session (D12)
    ts: float
    session_date: str                      # ET session this anchor belongs to, "YYYY-MM-DD"
    history: deque[tuple[float, float]]    # bounded ring buffer of (ts, price)

    # ---- derived ----------------------------------------------------
    @property
    def change(self) -> float:
        """Tick delta — drives the flash, not the daily-change column."""
        return self.price - self.prev_price

    @property
    def direction(self) -> Direction:
        if self.price > self.prev_price:
            return "up"
        return "down" if self.price < self.prev_price else "flat"

    @property
    def day_change(self) -> float:
        return self.price - self.open_price

    @property
    def day_change_pct(self) -> float:
        if not self.open_price:            # guards a pre-market 0.0 anchor (Review B5)
            return 0.0
        return (self.price - self.open_price) / self.open_price * 100.0

    # ---- wire -------------------------------------------------------
    def to_wire(self) -> dict[str, object]:
        """snake_case, epoch-ms, display-rounded. The only place rounding happens."""
        return {
            "ticker": self.ticker,
            "price": round(self.price, 4),
            "prev_price": round(self.prev_price, 4),
            "open_price": round(self.open_price, 4),
            "change": round(self.change, 4),
            "change_pct": round(self.day_change_pct, 3),
            "direction": self.direction,
            # round, not int: float seconds x 1000 lands just under the whole millisecond
            # often enough that truncation would silently shed 1ms on arbitrary ticks.
            "ts": round(self.ts * 1000),
        }
